from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .models import ContactorFeedback, Measurement, ProtectionEvent
from .protection import ProtectionConfig, ProtectionController


class JsonlEventRecorder:
    """Append replayable measurements and protection decisions as JSONL."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record_measurement(self, measurement: Measurement) -> None:
        self._write({"record": "measurement", **measurement.to_dict()})

    def record_events(self, events: Iterable[ProtectionEvent]) -> None:
        for event in events:
            self._write({"record": "event", **event.to_dict()})

    def record_feedback(
        self,
        controller: ProtectionController,
        feedback: ContactorFeedback,
    ) -> list[ProtectionEvent]:
        events = controller.update_feedback(feedback)
        self._write({"record": "feedback", **feedback.to_dict()})
        self.record_events(events)
        return events

    def record_controller_state(self, controller: ProtectionController) -> None:
        self._write({"record": "controller_state", **controller.state_snapshot()})

    def record_reset(
        self, controller: ProtectionController, timestamp: float | None = None
    ) -> list[ProtectionEvent]:
        events = controller.request_reset(timestamp)
        self.record_events(events)
        self.record_controller_state(controller)
        return events

    def record_repower(
        self, controller: ProtectionController, timestamp: float
    ) -> list[ProtectionEvent]:
        events = controller.repower(timestamp)
        self.record_events(events)
        self.record_controller_state(controller)
        return events

    def _write(self, payload: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            stream.write("\n")


def replay(path: str | Path) -> list[dict[str, object]]:
    with Path(path).open("r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def restore_controller(
    path: str | Path, config: ProtectionConfig | None = None
) -> ProtectionController:
    controller = ProtectionController(config)
    snapshots = [row for row in replay(path) if row["record"] == "controller_state"]
    if snapshots:
        controller.restore_state(snapshots[-1])
    return controller


def replay_recording(
    path: str | Path, config: ProtectionConfig | None = None
) -> list[dict[str, object]]:
    controller = ProtectionController(config)
    trace: list[dict[str, object]] = []
    rows = [row for row in replay(path) if row["record"] != "controller_state"]
    index = 0

    while index < len(rows):
        row = rows[index]
        if row["record"] == "measurement":
            timestamp = float(row["timestamp"])
            measurement = Measurement(
                timestamp=timestamp,
                voltages=list(row["voltages"]),
                temperatures=list(row["temperatures"]),
            )
            produced = controller.update(measurement)
            expected = _take_following_events(rows, index)
            actual = [event.to_dict() for event in produced]
            expected_payloads = [
                {key: value for key, value in event.items() if key != "record"}
                for event in expected
            ]
            if (
                not controller.events[:-len(produced)]
                and produced
                and produced[-1].event_type == "initial_state"
                and expected_payloads
                and expected_payloads[-1]["event_type"] == "contactor_state"
            ):
                actual[-1]["event_type"] = "contactor_state"
            if actual != expected_payloads:
                raise ValueError(f"recorded decisions diverged at t={timestamp}")
            trace.append(_trace_point(controller, timestamp))
            index += 1 + len(expected)
            continue

        if row["record"] == "event" and row["event_type"] in {
            "manual_reset",
            "reset_blocked",
            "reset_idle",
        }:
            timestamp = float(row["timestamp"])
            produced = controller.request_reset(timestamp)
            actual = [event.to_dict() for event in produced]
            if actual != [
                {key: value for key, value in row.items() if key != "record"}
            ]:
                raise ValueError(f"recorded reset decision diverged at t={timestamp}")
            trace.append(_trace_point(controller, timestamp))
            index += 1
            continue

        if row["record"] == "feedback":
            timestamp = float(row["timestamp"])
            feedback = ContactorFeedback(
                timestamp=timestamp,
                charge_closed=row["charge_closed"],
                discharge_closed=row["discharge_closed"],
            )
            produced = controller.update_feedback(feedback)
            expected = _take_following_events(rows, index)
            actual = [event.to_dict() for event in produced]
            expected_payloads = [
                {key: value for key, value in event.items() if key != "record"}
                for event in expected
            ]
            if (
                len(controller.events) == len(produced)
                and actual
                and actual[-1]["event_type"] == "initial_state"
                and expected_payloads
                and expected_payloads[-1]["event_type"] == "contactor_state"
            ):
                actual[-1]["event_type"] = "contactor_state"
            if actual != expected_payloads:
                raise ValueError(f"recorded feedback decision diverged at t={timestamp}")
            trace.append(_trace_point(controller, timestamp))
            index += 1 + len(expected)
            continue

        if row["record"] == "event" and row["event_type"] in {
            "repower_blocked",
            "power_on_ready",
        }:
            timestamp = float(row["timestamp"])
            produced = controller.repower(timestamp)
            actual = [event.to_dict() for event in produced]
            if actual != [
                {key: value for key, value in row.items() if key != "record"}
            ]:
                raise ValueError(f"recorded repower decision diverged at t={timestamp}")
            trace.append(_trace_point(controller, timestamp))
            index += 1
            continue

        index += 1

    return trace


def _take_following_events(
    rows: list[dict[str, object]], measurement_index: int
) -> list[dict[str, object]]:
    events = []
    index = measurement_index + 1
    while (
        index < len(rows)
        and rows[index]["record"] == "event"
        and rows[index]["event_type"]
        not in {
            "manual_reset",
            "reset_blocked",
            "reset_idle",
            "repower_blocked",
            "power_on_ready",
        }
    ):
        events.append(rows[index])
        index += 1
    return events


def _trace_point(
    controller: ProtectionController, timestamp: float
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "charge_contactor_closed": controller.charge_contactor_closed,
        "discharge_contactor_closed": controller.discharge_contactor_closed,
        "active_reasons": controller.active_reasons,
        "reset_ready_reasons": controller.reset_ready_reasons,
        "reset_blocked_reasons": controller.reset_blocked_reasons,
    }
