from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .models import Measurement, ProtectionEvent


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
    with Path(path).open("r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]
