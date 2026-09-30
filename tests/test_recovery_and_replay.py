from battery_platform.models import Measurement
from battery_platform.protection import (
    FaultRule,
    ProtectionConfig,
    ProtectionController,
)
from battery_platform.recorder import (
    JsonlEventRecorder,
    replay,
    replay_protection,
)


def sample(timestamp, value=3.7, temperature=30.0):
    return Measurement(
        timestamp,
        [value, 3.7, 3.7, 3.7],
        [temperature, 30.0, 30.0, 30.0],
    )


def trigger_over_voltage(controller):
    controller.update(sample(0.0))
    controller.update(sample(1.0, value=4.25))
    controller.update(sample(2.0, value=4.25))


def test_hysteresis_value_above_recovery_threshold_blocks_reset():
    controller = ProtectionController()
    trigger_over_voltage(controller)

    # 4.15 is below the 4.20 trigger but above the 4.10 recovery threshold;
    # it must neither count as recovery nor allow contactor closure.
    controller.update(sample(3.0, value=4.15))
    reset = controller.request_reset(3.0)

    assert reset[0].event_type == "reset_blocked"
    assert reset[0].reasons == ()
    assert "over_voltage" in reset[0].details["blocked_reasons"]
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is True
    assert "over_voltage" in controller.active_reasons

    controller.update(sample(4.0, value=4.15))
    assert controller.request_reset(4.0)[0].event_type == "reset_blocked"
    assert controller.charge_contactor_closed is False


def test_recovery_timer_restarts_when_hysteresis_interrupts_it():
    config = ProtectionConfig(
        over_voltage=FaultRule(4.20, 4.10, 1.0, 1.0)
    )
    controller = ProtectionController(config)
    trigger_over_voltage(controller)

    controller.update(sample(3.0, value=4.05))  # safe, recovery starts
    controller.update(sample(3.8, value=4.05))
    interrupted = controller.update(sample(4.0, value=4.15))  # leaves safe
    assert interrupted[0].event_type == "recovery_interrupted"
    assert interrupted[0].reasons == ("over_voltage",)

    # Only 0.8 s of a fresh recovery window elapsed; reset must stay blocked.
    controller.update(sample(4.2, value=4.05))
    controller.update(sample(5.0, value=4.05))
    assert controller.request_reset(5.0)[0].event_type == "reset_blocked"
    assert controller.charge_contactor_closed is False

    ready = controller.update(sample(5.3, value=4.05))
    assert ready[0].event_type == "recovery_ready_manual_reset_required"
    reset = controller.request_reset(5.3)
    assert reset[0].event_type == "manual_reset"
    assert controller.charge_contactor_closed is True


def test_fault_returning_after_recovery_ready_relocks_and_retriggers():
    controller = ProtectionController()
    trigger_over_voltage(controller)
    controller.update(sample(3.0, value=4.05))
    ready = controller.update(sample(4.0, value=4.05))
    assert ready[0].event_type == "recovery_ready_manual_reset_required"

    # Slipping back into the hysteresis band cancels recovery readiness.
    controller.update(sample(4.5, value=4.15))
    assert controller.request_reset(4.5)[0].event_type == "reset_blocked"
    assert controller.charge_contactor_closed is False

    # Crossing the trigger again for a full delay latches the fault anew.
    controller.update(sample(5.0, value=4.25))
    retrigger = controller.update(sample(6.0, value=4.25))
    assert retrigger[0].event_type == "protection_triggered"
    assert controller.charge_contactor_closed is False


def test_concurrent_faults_keep_independent_latches():
    config = ProtectionConfig(
        over_voltage=FaultRule(4.20, 4.10, 1.0, 1.0),
        under_voltage=FaultRule(3.00, 3.10, 1.0, 5.0, direction="below"),
    )
    controller = ProtectionController(config)
    controller.update(sample(0.0))
    controller.update(sample(1.0, value=4.25))
    controller.update(sample(2.0, value=4.25))  # over-voltage latches

    under = lambda t: Measurement(
        t, [2.90, 3.7, 3.7, 3.7], [30.0] * 4
    )
    controller.update(under(3.0))
    events_at_under_trigger = controller.update(under(4.0))
    under_trigger = next(
        event
        for event in events_at_under_trigger
        if event.event_type == "protection_triggered"
        and event.reasons == ("under_voltage",)
    )
    assert under_trigger.event_type == "protection_triggered"
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is False
    assert set(controller.active_reasons) == {
        "over_voltage",
        "under_voltage",
    }

    # Both become safe at t=5. Over-voltage recovers in 1 s, under-voltage
    # needs 5 s, so only over-voltage can be reset at t=6.
    controller.update(Measurement(5.0, [3.7] * 4, [30.0] * 4))
    controller.update(Measurement(6.0, [3.7] * 4, [30.0] * 4))
    assert any(
        event.event_type == "recovery_ready_manual_reset_required"
        and event.reasons == ("over_voltage",)
        for event in controller.events
    )
    partial = controller.request_reset(6.0)
    assert partial[0].event_type == "manual_reset"
    assert partial[0].reasons == ("over_voltage",)
    assert "under_voltage" in partial[0].details["blocked_reasons"]
    assert controller.charge_contactor_closed is True
    assert controller.discharge_contactor_closed is False
    assert controller.active_reasons == ("under_voltage",)

    assert controller.request_reset(6.1)[0].event_type == "reset_blocked"


def test_replay_rebuilds_contactors_with_missing_and_truncated_tail(tmp_path):
    path = tmp_path / "log.jsonl"
    recorder = JsonlEventRecorder(path)
    controller = ProtectionController()

    sequence = [
        sample(0.0),
        sample(1.0, value=4.25),
        sample(2.0, value=4.25),
        sample(3.0, value=4.15),
        Measurement(4.0, [None, 3.7, 3.7, 3.7], [30.0] * 4),
        sample(5.0, value=4.05),
        sample(5.5, value=4.05),
    ]
    for measurement in sequence:
        recorder.record_measurement(measurement)
        recorder.record_events(controller.update(measurement))
    recorder.record_events(controller.request_reset(5.5))

    # Simulate a process restart interrupting the final write.
    text = path.read_text(encoding="utf-8")
    path.write_text(text + '{"record": "measurement", "timesta', encoding="utf-8")

    rows = replay(path)
    assert rows[-1]["record"] == "event"  # truncated line is ignored

    result = replay_protection(path)
    assert result.measurements == 7
    assert result.consistent, [
        (mismatch.timestamp, mismatch.field, mismatch.recorded, mismatch.replayed)
        for mismatch in result.mismatches
    ]

    # Both faults recovered with their own timing and are released together.
    reset_event = next(
        row
        for row in reversed(replay(path))
        if row["record"] == "event"
    )
    assert reset_event["event_type"] == "manual_reset"
    assert set(reset_event["reasons"]) == {
        "over_voltage",
        "measurement_fault",
    }
    assert result.controller.charge_contactor_closed is True
    assert result.controller.discharge_contactor_closed is True


def test_replay_detects_false_unlock_written_in_log(tmp_path):
    path = tmp_path / "log.jsonl"
    recorder = JsonlEventRecorder(path)
    controller = ProtectionController()
    for measurement in [sample(0.0), sample(1.0, 4.25), sample(2.0, 4.25)]:
        recorder.record_measurement(measurement)
        recorder.record_events(controller.update(measurement))

    # A stale/buggy writer claims the contactor re-opened (closed again) at
    # 4.15 V; replay must detect the contradiction instead of trusting it.
    bad = sample(3.0, value=4.15)
    recorder.record_measurement(bad)
    controller.update(bad)
    tampered = (
        '{"record":"event","timestamp":3.0,'
        '"event_type":"contactor_state",'
        '"charge_contactor_closed":true,'
        '"discharge_contactor_closed":true,'
        '"active_reasons":[],"locked_reasons":[],"reasons":[],'
        '"details":{}}\n'
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(tampered)

    result = replay_protection(path)
    assert not result.consistent
    assert any(mismatch.field == "event" for mismatch in result.mismatches)
    assert result.controller.charge_contactor_closed is False
