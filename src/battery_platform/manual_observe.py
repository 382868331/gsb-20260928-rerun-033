from __future__ import annotations

import argparse

from .protection import ProtectionController
from .recorder import JsonlEventRecorder
from .simulator import VirtualBatteryPack


def run_demo(output: str = "protection_log.jsonl", cycles: int = 4) -> None:
    pack = VirtualBatteryPack(time_step=0.2)
    controller = ProtectionController()
    recorder = JsonlEventRecorder(output)

    for measurement in pack.boundary_jitter_sequence(cycles):
        recorder.record_measurement(measurement)
        events = controller.update(measurement)
        recorder.record_events(events)

    print("边界抖动期间有效事件：")
    for event in controller.replay():
        if event.event_type in {
            "protection_triggered",
            "recovery_ready_manual_reset_required",
            "protection_released",
            "manual_reset",
            "reset_blocked",
        }:
            print(
                f"t={event.timestamp:.1f} {event.event_type} "
                f"reasons={','.join(event.reasons) or '-'} "
                f"charge={event.charge_contactor_closed} "
                f"discharge={event.discharge_contactor_closed}"
            )
            if event.details.get("recovery_threshold") is not None:
                print(f"  恢复条件: 阈值 {event.details['recovery_threshold']}，"
                      f"持续 {event.details['recovery_delay']}s 后人工复位")
    print(f"完整原始测量与决策记录: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Observe protection boundary jitter")
    parser.add_argument("--output", default="protection_log.jsonl")
    parser.add_argument("--cycles", type=int, default=4)
    args = parser.parse_args()
    run_demo(args.output, args.cycles)


if __name__ == "__main__":
    main()
