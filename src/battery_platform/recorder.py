from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
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

    def _write(self, payload: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            stream.write("\n")


def replay(path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A crash mid-write can leave a truncated final record; earlier
            # complete records must still be replayable.
            if index != len(lines) - 1:
                raise
    return rows


@dataclass(frozen=True)
class ReplayMismatch:
    timestamp: float
    field: str
    recorded: object
    replayed: object


@dataclass
class ReplayResult:
    controller: ProtectionController
    measurements: int
    mismatches: list[ReplayMismatch]

    @property
    def consistent(self) -> bool:
        return not self.mismatches


_COMPARED_FIELDS = (
    "event_type",
    "charge_contactor_closed",
    "discharge_contactor_closed",
    "active_reasons",
    "locked_reasons",
    "reasons",
)


def replay_protection(
    path: str | Path, config: ProtectionConfig | None = None
) -> ReplayResult:
    """Rebuild decisions from raw measurements and verify every event.

    Recorded decisions are not trusted: measurements are replayed through a
    fresh controller and each logged event is compared field by field,
    including contactor state and active/locked reasons at that timestamp.
    Manual reset records are reproduced via :meth:`request_reset`.
    """

    controller = ProtectionController(config)
    mismatches: list[ReplayMismatch] = []
    pending: list[ProtectionEvent] = []
    measurement_count = 0

    def compare(recorded: dict[str, object], replayed: ProtectionEvent) -> None:
        for field_name in _COMPARED_FIELDS:
            expected = recorded.get(field_name)
            if isinstance(expected, list):
                expected = tuple(expected)
            actual = getattr(replayed, field_name)
            if expected != actual:
                mismatches.append(
                    ReplayMismatch(
                        timestamp=float(recorded.get("timestamp", 0.0)),
                        field=field_name,
                        recorded=expected,
                        replayed=actual,
                    )
                )

    for row in replay(path):
        kind = row.get("record")
        if kind == "measurement":
            measurement_count += 1
            pending.extend(
                controller.update(
                    Measurement(
                        timestamp=float(row["timestamp"]),
                        voltages=list(row["voltages"]),
                        temperatures=list(row["temperatures"]),
                    )
                )
            )
        elif kind == "event":
            event_type = row.get("event_type")
            if event_type in {"manual_reset", "reset_blocked", "reset_idle"}:
                replayed = controller.request_reset(float(row["timestamp"]))
                compare(row, replayed[0])
                continue
            if not pending:
                mismatches.append(
                    ReplayMismatch(
                        timestamp=float(row.get("timestamp", 0.0)),
                        field="event",
                        recorded=event_type,
                        replayed=None,
                    )
                )
                continue
            compare(row, pending.pop(0))

    return ReplayResult(controller, measurement_count, mismatches)
