from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .models import Measurement, ProtectionEvent
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

    def record_controller_state(self, controller: ProtectionController) -> None:
        self._write({"record": "controller_state", **controller.state_snapshot()})

    def record_reset(
        self, controller: ProtectionController, timestamp: float | None = None
    ) -> list[ProtectionEvent]:
        events = controller.request_reset(timestamp)
        self.record_events(events)
        self.record_controller_state(controller)
        return events

    def record_replacement(
        self,
        controller: ProtectionController,
        contactor_name: str,
        new_part_id: str,
        timestamp: float | None = None,
        note: str = "",
    ) -> ProtectionEvent:
        event = controller.register_contactor_replacement(
            contactor_name, new_part_id, timestamp, note
        )
        self.record_events([event])
        self.record_controller_state(controller)
        return event

    def record_retraction(
        self,
        controller: ProtectionController,
        contactor_name: str,
        timestamp: float | None = None,
        reason: str = "",
    ) -> ProtectionEvent:
        event = controller.retract_contactor_replacement(
            contactor_name, timestamp, reason
        )
        self.record_events([event])
        self.record_controller_state(controller)
        return event

    def record_part_feedback(
        self,
        controller: ProtectionController,
        contactor_name: str,
        part_id: str | None,
        observed_closed: bool | None,
        timestamp: float | None = None,
    ) -> list[ProtectionEvent]:
        events = controller.register_part_feedback(
            contactor_name, part_id, observed_closed, timestamp
        )
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
                charge_contactor_feedback=row.get("charge_contactor_feedback"),
                discharge_contactor_feedback=row.get(
                    "discharge_contactor_feedback"
                ),
                load_voltage=row.get("load_voltage"),
            )
            produced = controller.update(measurement)
            expected = _take_following_events(rows, index)
            actual = [event.to_dict() for event in produced]
            expected_payloads = [
                {key: value for key, value in event.items() if key != "record"}
                for event in expected
            ]
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

        if row["record"] == "event" and row["event_type"] in {
            "contactor_replaced",
            "contactor_replacement_retracted",
        }:
            timestamp = float(row["timestamp"])
            details = row["details"]
            if row["event_type"] == "contactor_replaced":
                produced = [
                    controller.register_contactor_replacement(
                        details["contactor"],
                        details["new_part_id"],
                        timestamp,
                        details.get("note", ""),
                    )
                ]
            else:
                produced = [
                    controller.retract_contactor_replacement(
                        details["contactor"],
                        timestamp,
                        details.get("reason", ""),
                    )
                ]
            actual = [event.to_dict() for event in produced]
            if actual != [
                {key: value for key, value in row.items() if key != "record"}
            ]:
                raise ValueError(
                    f"recorded replacement decision diverged at t={timestamp}"
                )
            trace.append(_trace_point(controller, timestamp))
            index += 1
            continue

        if row["record"] == "event" and row["event_type"] in {
            "part_feedback_applied",
            "late_part_feedback",
            "part_feedback_pending",
        }:
            timestamp = float(row["timestamp"])
            details = row["details"]
            produced = controller.register_part_feedback(
                details["contactor"],
                details.get("part_id"),
                details.get("observed_closed"),
                timestamp,
            )
            expected = [row] + _take_following_events(rows, index)
            actual = [event.to_dict() for event in produced]
            expected_payloads = [
                {key: value for key, value in event.items() if key != "record"}
                for event in expected
            ]
            if actual != expected_payloads:
                raise ValueError(
                    f"recorded part feedback decision diverged at t={timestamp}"
                )
            trace.append(_trace_point(controller, timestamp))
            index += len(expected)
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
            "contactor_replaced",
            "contactor_replacement_retracted",
            "part_feedback_applied",
            "late_part_feedback",
            "part_feedback_pending",
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
        "charge_contactor_observed_closed": (
            controller.charge_contactor_observed_closed
        ),
        "discharge_contactor_observed_closed": (
            controller.discharge_contactor_observed_closed
        ),
        "active_reasons": controller.active_reasons,
        "reset_ready_reasons": controller.reset_ready_reasons,
        "reset_blocked_reasons": controller.reset_blocked_reasons,
        "post_disconnect_verification": (
            controller.post_disconnect_verification
        ),
        "current_parts": controller.current_parts,
        "part_generations": controller.part_generations,
        "pending_feedback": controller.pending_feedback,
    }
