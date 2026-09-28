from battery_protection.config import ProtectionConfig
from battery_protection.controller import ProtectionController
from battery_protection.model import Measurement, ProtectionKind
from battery_protection.recorder import JsonlRecorder, Replayer
from battery_protection.simulator import VirtualBatteryPack


def measurement(timestamp, voltages=(3.7, 3.7), temperatures=(25.0,)):
    return Measurement(timestamp, tuple(voltages), tuple(temperatures))


def test_single_spike_does_not_trip():
    controller = ProtectionController(ProtectionConfig.default(trigger_seconds=1.0))

    assert not controller.update(measurement(0.0, voltages=(4.2, 3.7))).contactor_open
    assert not controller.update(measurement(0.5, voltages=(4.2, 3.7))).contactor_open
    assert not controller.update(measurement(1.0, voltages=(4.0, 3.7))).contactor_open


def test_persistent_overvoltage_trips_and_uses_release_band():
    controller = ProtectionController(ProtectionConfig.default(
        ov_trigger=4.2, ov_release=4.1, trigger_seconds=1.0, release_seconds=1.0
    ))

    controller.update(measurement(0.0, voltages=(4.2, 3.7)))
    assert not controller.is_active(ProtectionKind.OVER_VOLTAGE)
    decision = controller.update(measurement(1.0, voltages=(4.2, 3.7)))
    assert decision.contactor_open
    assert decision.events[0].kind is ProtectionKind.OVER_VOLTAGE
    assert "< 4.1" in decision.events[0].recovery_condition

    assert controller.update(measurement(1.5, voltages=(4.09, 3.7))).contactor_open
    recovered = controller.update(measurement(2.5, voltages=(4.09, 3.7)))
    assert not recovered.contactor_open
    assert recovered.events[0].action == "recover"


def test_boundary_jitter_does_not_repeatedly_open_and_close():
    controller = ProtectionController(ProtectionConfig.default(
        ov_trigger=4.2, ov_release=4.1, trigger_seconds=1.0, release_seconds=2.0
    ))
    controller.update(measurement(0.0, voltages=(4.2, 3.7)))
    decision = controller.update(measurement(1.0, voltages=(4.2, 3.7)))
    assert decision.events[0].action == "trip"

    actions = []
    for index, voltage in enumerate((4.15, 4.2, 4.12, 4.2, 4.18), start=1):
        decision = controller.update(measurement(1.0 + index, voltages=(voltage, 3.7)))
        actions.extend(event.action for event in decision.events)

    assert actions == []
    assert controller.is_active(ProtectionKind.OVER_VOLTAGE)


def test_undervoltage_and_overtemperature_are_separate_reasons():
    config = ProtectionConfig.default(trigger_seconds=0.5, release_seconds=0.5)
    controller = ProtectionController(config)

    first = controller.update(measurement(0.0, voltages=(2.8, 3.7), temperatures=(55.0,)))
    second = controller.update(measurement(0.5, voltages=(2.8, 3.7), temperatures=(55.0,)))
    assert first.events == ()
    assert {event.kind for event in second.events} == {
        ProtectionKind.UNDER_VOLTAGE,
        ProtectionKind.OVER_TEMPERATURE,
    }
    assert set(second.active_protections) == {
        ProtectionKind.UNDER_VOLTAGE,
        ProtectionKind.OVER_TEMPERATURE,
    }


def test_missing_measurement_is_not_normal_and_trips():
    controller = ProtectionController(ProtectionConfig.default(missing_trigger_seconds=0.5))
    controller.update(measurement(0.0, voltages=(None, 3.7)))
    decision = controller.update(measurement(0.5, voltages=(None, 3.7)))

    assert not decision.measurement_valid
    assert ProtectionKind.MEASUREMENT_INVALID in decision.active_protections
    assert "missing" in decision.events[0].reason


def test_latched_protection_requires_manual_reset_after_release_condition():
    config = ProtectionConfig.default(
        trigger_seconds=0.0,
        release_seconds=1.0,
        latch_over_temperature=True,
    )
    controller = ProtectionController(config)
    tripped = controller.update(measurement(0.0, temperatures=(55.0,)))
    assert tripped.events[0].latch
    assert controller.is_locked(ProtectionKind.OVER_TEMPERATURE)

    controller.update(measurement(1.0, temperatures=(49.0,)))
    rejected = controller.reset(ProtectionKind.OVER_TEMPERATURE, measurement(1.0, temperatures=(49.0,)))
    assert rejected.action == "reset_rejected"
    assert controller.is_active(ProtectionKind.OVER_TEMPERATURE)

    accepted = controller.reset(ProtectionKind.OVER_TEMPERATURE, measurement(2.0, temperatures=(49.0,)))
    assert accepted.action == "reset_and_recovered"
    assert not controller.is_active(ProtectionKind.OVER_TEMPERATURE)
    assert not controller.is_locked(ProtectionKind.OVER_TEMPERATURE)


def test_records_are_replayable(tmp_path):
    path = tmp_path / "run.jsonl"
    config = ProtectionConfig.default(trigger_seconds=0.5, release_seconds=0.5)
    source = ProtectionController(config)
    replay = ProtectionController(config)
    pack = VirtualBatteryPack(1)

    with JsonlRecorder(path) as recorder:
        for index, voltage in enumerate((4.2, 4.2, 4.0, 4.0)):
            pack.set_voltage(0, voltage)
            observed = pack.measure(index * 0.5)
            decision = source.update(observed)
            recorder.write_measurement(observed)
            recorder.write_decision(decision)
            for event in decision.events:
                recorder.write_event(event)

    replay_actions = []
    for record in Replayer(path):
        if record["type"] == "measurement":
            decision = replay.update(Replayer.measurement_from(record))
            replay_actions.extend((event.action, event.kind.value) for event in decision.events)

    saved_events = [(item["action"], item["kind"]) for item in Replayer(path).events()]
    assert replay_actions == saved_events
    assert replay_actions == [("trip", "over_voltage"), ("recover", "over_voltage")]
    trip = next(item for item in Replayer(path).events() if item["action"] == "trip")
    assert trip["recovery_condition"] == "all voltages < 4.1 for 0.5s"
    assert trip["measurement"]["cell_voltages"] == [4.2]
