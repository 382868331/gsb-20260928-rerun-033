import json

from battery_platform.protection import ProtectionController
from battery_platform.models import Measurement
from battery_platform.recorder import (
    JsonlEventRecorder,
    replay,
    replay_recording,
    restore_controller,
)
from battery_platform.simulator import VirtualBatteryPack


def test_simulator_emits_raw_measurements_not_decisions(tmp_path):
    pack = VirtualBatteryPack(cell_count=2)
    measurement = pack.sample()

    assert measurement.voltages == [3.7, 3.7]
    assert not hasattr(measurement, "protection_active")


def test_jsonl_recorder_is_replayable(tmp_path):
    path = tmp_path / "log.jsonl"
    recorder = JsonlEventRecorder(path)
    pack = VirtualBatteryPack(cell_count=4, time_step=1.0)
    controller = ProtectionController()

    first = pack.sample()
    recorder.record_measurement(first)
    recorder.record_events(controller.update(first))
    second = pack.sample(lambda index, timestamp: 4.25 if index == 0 else 3.7)
    recorder.record_measurement(second)
    recorder.record_events(controller.update(second))

    rows = replay(path)
    assert [row["record"] for row in rows[:2]] == ["measurement", "event"]
    assert all(json.loads(line) for line in path.read_text().splitlines())


def test_recording_replays_per_sample_permissions_across_restart(tmp_path):
    path = tmp_path / "trace.jsonl"
    recorder = JsonlEventRecorder(path)
    controller = ProtectionController()

    samples = [
        Measurement(0.0, [3.7] * 4, [30.0] * 4),
        Measurement(1.0, [4.21] + [3.7] * 3, [30.0] * 4),
        Measurement(2.0, [4.21] + [3.7] * 3, [30.0] * 4),
        Measurement(2.5, [4.05] + [3.7] * 3, [30.0] * 4),
        Measurement(3.0, [4.15] + [3.7] * 3, [30.0] * 4),
        Measurement(3.5, [None] + [3.7] * 3, [30.0] * 4),
        Measurement(4.0, [4.05] + [3.7] * 3, [30.0] * 4),
    ]
    for index, measurement in enumerate(samples[:5]):
        recorder.record_measurement(measurement)
        recorder.record_events(controller.update(measurement))
        recorder.record_controller_state(controller)

    restarted = restore_controller(path)
    blocked = recorder.record_reset(restarted, 3.0)
    assert blocked[0].event_type == "reset_blocked"
    assert restarted.charge_contactor_closed is False

    for measurement in samples[5:]:
        recorder.record_measurement(measurement)
        recorder.record_events(restarted.update(measurement))
        recorder.record_controller_state(restarted)

    trace = replay_recording(path)
    contactor_states = [
        (
            point["charge_contactor_closed"],
            point["discharge_contactor_closed"],
        )
        for point in trace[:3]
    ]
    assert contactor_states == [(True, True), (True, True), (False, True)]
    assert trace[3]["reset_blocked_reasons"] == ("over_voltage",)
    assert trace[7] == {
        "timestamp": 4.0,
        "charge_contactor_closed": False,
        "discharge_contactor_closed": False,
        "charge_contactor_observed_closed": None,
        "discharge_contactor_observed_closed": None,
        "active_reasons": (
            "over_voltage",
            "measurement_fault",
            "post_disconnect_voltage_verification",
        ),
        "reset_ready_reasons": (),
        "reset_blocked_reasons": (
            "over_voltage",
            "measurement_fault",
            "post_disconnect_voltage_verification",
        ),
        "current_parts": {
            "charge": "charge-original",
            "discharge": "discharge-original",
        },
        "post_disconnect_verification": {
            "required": True,
            "active": True,
            "locked": False,
            "recovery_ready": False,
            "both_open_since": None,
            "low_voltage_since": None,
            "failure_reason": "waiting_for_contactor_feedback:charge,discharge",
            "last_voltage": None,
            "actual_disconnect_contactor": None,
            "last_opened_contactor": None,
            "low_voltage_observed": False,
        },
    }
