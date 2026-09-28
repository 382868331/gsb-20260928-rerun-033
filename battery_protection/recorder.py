from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .controller import ProtectionDecision, ProtectionEvent
from .model import Measurement, ProtectionKind


class JsonlRecorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")

    def write_measurement(self, measurement: Measurement) -> None:
        self._write("measurement", self._measurement_dict(measurement))

    def write_event(self, event: ProtectionEvent) -> None:
        self._write("event", self._event_dict(event))

    def write_decision(self, decision: ProtectionDecision) -> None:
        payload = {
            "timestamp": decision.timestamp,
            "contactor_open": decision.contactor_open,
            "active_protections": [kind.value for kind in decision.active_protections],
            "locked_protections": [kind.value for kind in decision.locked_protections],
            "measurement_valid": decision.measurement_valid,
            "violations": [
                {
                    "kind": violation.kind.value,
                    "detail": violation.detail,
                    "value": violation.value,
                }
                for violation in decision.violations
            ],
            "events": [self._event_dict(event) for event in decision.events],
            "measurement": self._measurement_dict(decision.measurement),
        }
        self._write("decision", payload)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "JsonlRecorder":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _write(self, record_type: str, payload: dict[str, Any]) -> None:
        self._file.write(json.dumps({"type": record_type, **payload}, ensure_ascii=False, sort_keys=True) + "\n")
        self._file.flush()

    @staticmethod
    def _measurement_dict(measurement: Measurement) -> dict[str, Any]:
        return {
            "timestamp": measurement.timestamp,
            "cell_voltages": list(measurement.cell_voltages),
            "temperatures": list(measurement.temperatures),
            "source": measurement.source,
            "metadata": measurement.metadata,
        }

    def _event_dict(self, event: ProtectionEvent) -> dict[str, Any]:
        return {
            "timestamp": event.timestamp,
            "action": event.action,
            "kind": event.kind.value,
            "reason": event.reason,
            "recovery_condition": event.recovery_condition,
            "detail": event.detail,
            "latch": event.latch,
            "measurement": self._measurement_dict(event.measurement),
        }


class Replayer:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL on line {line_number}") from exc

    def measurements(self) -> Iterator[Measurement]:
        for record in self:
            if record["type"] == "measurement":
                yield self.measurement_from(record)

    def events(self) -> Iterator[dict[str, Any]]:
        for record in self:
            if record["type"] == "event":
                yield record

    @staticmethod
    def measurement_from(record: dict[str, Any]) -> Measurement:
        return Measurement(
            timestamp=float(record["timestamp"]),
            cell_voltages=tuple(record["cell_voltages"]),
            temperatures=tuple(record["temperatures"]),
            source=record.get("source", "replay"),
            metadata=dict(record.get("metadata", {})),
        )

    @staticmethod
    def kind(value: str) -> ProtectionKind:
        return ProtectionKind(value)
