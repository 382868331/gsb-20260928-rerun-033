import json

from battery_platform.protection import ProtectionController
from battery_platform.recorder import JsonlEventRecorder, replay
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
