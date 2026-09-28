from __future__ import annotations

import argparse
from pathlib import Path

from .config import ProtectionConfig
from .controller import ProtectionController
from .model import Measurement
from .recorder import JsonlRecorder, Replayer
from .simulator import VirtualBatteryPack


def run_jitter_demo(output: Path, *, dt: float = 0.2, steps: int = 80) -> list[tuple[Measurement, object]]:
    output.unlink(missing_ok=True)
    config = ProtectionConfig.default(
        trigger_seconds=1.0,
        release_seconds=1.0,
        missing_trigger_seconds=0.4,
    )
    controller = ProtectionController(config)
    pack = VirtualBatteryPack(cell_count=2)
    history: list[tuple[Measurement, object]] = []

    with JsonlRecorder(output) as recorder:
        for index in range(steps):
            timestamp = index * dt
            pack.set_voltage(0, 4.20 if index % 2 == 0 else 4.099)
            if 20 <= index < 24:
                pack.set_temperature(0, None)
            elif 30 <= index:
                pack.set_temperature(0, 55.0 if index % 4 < 2 else 49.9)
            measurement = pack.measure(timestamp)
            decision = controller.update(measurement)
            recorder.write_measurement(measurement)
            for event in decision.events:
                recorder.write_event(event)
            recorder.write_decision(decision)
            history.append((measurement, decision))
    return history


def replay(path: Path, config: ProtectionConfig) -> tuple[int, list[str]]:
    controller = ProtectionController(config)
    actions: list[str] = []
    records = 0
    for record in Replayer(path):
        if record["type"] != "measurement":
            continue
        records += 1
        decision = controller.update(Replayer.measurement_from(record))
        for event in decision.events:
            actions.append(f"{event.timestamp:.1f} {event.action} {event.kind.value}: {event.reason}")
    return records, actions


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a boundary-jitter protection observation demo")
    parser.add_argument("--output", type=Path, default=Path("protection-run.jsonl"))
    args = parser.parse_args()

    history = run_jitter_demo(args.output)
    print(f"recorded {len(history)} samples to {args.output}")
    for measurement, decision in history:
        if not isinstance(decision, object) or not getattr(decision, "events", ()):
            continue
        for event in decision.events:
            print(f"{measurement.timestamp:5.1f}s {event.action:18} {event.kind.value:20} {event.reason}")

    count, actions = replay(args.output, ProtectionConfig.default(
        trigger_seconds=1.0,
        release_seconds=1.0,
        missing_trigger_seconds=0.4,
    ))
    print(f"replayed {count} raw measurements; {len(actions)} replay events")
    for action in actions:
        print("replay:", action)


if __name__ == "__main__":
    main()
