from battery_platform.models import Measurement
from battery_platform.protection import (
    FaultRule,
    ProtectionConfig,
    ProtectionController,
)
from battery_platform.recorder import (
    JsonlEventRecorder,
    replay_recording,
    restore_controller,
)


def sample(
    timestamp,
    *,
    charge_feedback,
    discharge_feedback,
    load_voltage,
    value=4.21,
    temperature=30.0,
):
    return Measurement(
        timestamp,
        [value, 3.7, 3.7, 3.7],
        [temperature] * 4,
        charge_contactor_feedback=charge_feedback,
        discharge_contactor_feedback=discharge_feedback,
        load_voltage=load_voltage,
    )


def config():
    return ProtectionConfig(
        over_voltage=FaultRule(4.2, 4.1, 0.0, 0.0),
        contactor_mismatch=FaultRule(None, None, 0.5, 1.0),
        post_disconnect_voltage=FaultRule(5.0, 5.0, 0.0, 0.5),
    )


def controller():
    return ProtectionController(config())


def events(controller, event_type):
    return [
        event
        for event in controller.replay()
        if event.event_type == event_type
    ]


def weld_charge(c):
    """Run a scenario where the charge contactor welds shut."""
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0, value=3.7))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    c.update(sample(1.2, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    triggered = c.update(sample(1.8, charge_feedback=True,
                                discharge_feedback=True, load_voltage=48.0))
    assert triggered[0].reasons == ("charge_contactor_mismatch",)
    assert "charge_contactor_mismatch" in c.active_reasons


def test_single_replacement_keeps_old_fault_and_system_not_healthy():
    c = controller()
    weld_charge(c)

    event = c.register_contactor_replacement("charge", "charge-A2", 2.0,
                                             note="welded pole swapped")
    assert event.event_type == "contactor_replaced"
    assert event.details["old_part_id"] == "charge-original"
    assert event.details["new_part_id"] == "charge-A2"
    assert event.details["preserved_faults"] == ["charge_contactor_mismatch"]
    assert event.details["pre_replacement_evidence"]["charge_feedback"] is True

    history = c.part_history
    assert history[0]["part_id"] == "charge-original"
    assert history[0]["faults"] == ["charge_contactor_mismatch"]
    assert history[0]["fault_state"]["locked"] is True

    # The old welded fault does not stick to the new part ...
    assert "charge_contactor_mismatch" not in c.active_reasons
    assert c.current_parts["charge"] == "charge-A2"
    # ... but the system is not healthy: verification is still incomplete.
    assert "post_disconnect_voltage_verification" in c.active_reasons
    assert c.charge_contactor_closed is False

    # New part reports open, but voltage decay is not yet observed: no
    # re-energisation and no reset.
    c.update(sample(2.2, charge_feedback=False, discharge_feedback=False,
                    load_voltage=48.0, value=4.05))
    blocked = c.request_reset(2.2)
    assert "post_disconnect_voltage_verification" in blocked[0].details[
        "blocked_reasons"
    ]
    assert blocked[0].details["charge_part_id"] == "charge-A2"

    # Sustained low voltage against the new part completes verification.
    c.update(sample(2.4, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    c.update(sample(3.0, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    passed = events(c, "post_disconnect_verification_passed")
    assert passed
    assert passed[0].details["charge_part_id"] == "charge-A2"
    reset = c.request_reset(3.0)
    assert reset[0].event_type == "manual_reset"
    assert c.charge_contactor_closed is True


def test_other_contactor_fault_still_blocks_after_replacement():
    c = controller()
    # Both poles weld: the charge mismatch confirms first and forces both
    # poles open; the discharge pole then also fails to follow.
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0, value=3.7))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    triggered = c.update(sample(1.6, charge_feedback=True,
                                discharge_feedback=True, load_voltage=48.0))
    assert [event.reasons[0] for event in triggered if event.reasons] == [
        "charge_contactor_mismatch"
    ]
    triggered = c.update(sample(2.2, charge_feedback=True,
                                discharge_feedback=True, load_voltage=48.0))
    assert [event.reasons[0] for event in triggered if event.reasons] == []
    triggered = c.update(sample(2.8, charge_feedback=True,
                                discharge_feedback=True, load_voltage=48.0))
    assert [event.reasons[0] for event in triggered if event.reasons] == [
        "discharge_contactor_mismatch"
    ]

    c.register_contactor_replacement("charge", "charge-A2", 3.0)
    # New charge part behaves, but the discharge pole is still welded.
    c.update(sample(3.2, charge_feedback=False, discharge_feedback=True,
                    load_voltage=48.0, value=4.05))
    c.update(sample(3.6, charge_feedback=False, discharge_feedback=True,
                    load_voltage=4.0, value=4.05))
    assert not events(c, "post_disconnect_verification_passed")
    blocked = c.request_reset(3.6)
    assert "discharge_contactor_mismatch" in blocked[0].details[
        "blocked_reasons"
    ]
    assert c.charge_contactor_closed is False
    assert c.discharge_contactor_closed is False


def test_two_sequential_replacements_keep_separate_histories():
    c = controller()
    weld_charge(c)
    c.register_contactor_replacement("charge", "charge-A2", 2.0)
    c.register_contactor_replacement("discharge", "discharge-B2", 2.5)

    assert c.current_parts == {
        "charge": "charge-A2",
        "discharge": "discharge-B2",
    }
    retired = [entry["part_id"] for entry in c.part_history]
    assert retired == ["charge-original", "discharge-original"]
    charge_record, discharge_record = c.part_history
    assert charge_record["faults"] == ["charge_contactor_mismatch"]
    assert discharge_record["faults"] == []

    # Verification continues along the same stage with the new parts.
    c.update(sample(2.6, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    c.update(sample(3.2, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    passed = events(c, "post_disconnect_verification_passed")
    assert passed
    assert passed[0].details["charge_part_id"] == "charge-A2"
    assert passed[0].details["discharge_part_id"] == "discharge-B2"


def test_late_old_feedback_does_not_fault_the_new_part():
    c = controller()
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0, value=3.7))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    c.update(sample(1.2, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    # Charge pole welds; the confirmed mismatch forces both poles open.
    c.update(sample(1.8, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    c.register_contactor_replacement("charge", "charge-A2", 1.9)
    # The late open feedback arrives after the swap: it is attributed to the
    # new part and must not inherit the old part's confirmed mismatch.
    c.update(sample(2.0, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    c.update(sample(2.6, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    mismatches = [
        event
        for event in c.replay()
        if event.event_type == "protection_triggered"
        and event.reasons[0].endswith("_contactor_mismatch")
        and event.details["part_id"] == "charge-A2"
    ]
    assert mismatches == []
    assert events(c, "post_disconnect_verification_passed")


def test_brief_voltage_recovery_after_replacement_keeps_failure():
    c = controller()
    weld_charge(c)
    c.register_contactor_replacement("charge", "charge-A2", 2.0)
    c.update(sample(2.2, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    # Voltage briefly recovers before the decay observation completes.
    recovered = c.update(sample(2.4, charge_feedback=False,
                                discharge_feedback=False, load_voltage=48.0,
                                value=4.05))
    assert recovered[-1].details["failure_reason"] == "voltage_recovered"
    blocked = c.request_reset(2.4)
    assert "post_disconnect_voltage_verification" in blocked[0].details[
        "blocked_reasons"
    ]
    assert blocked[0].details["charge_part_id"] == "charge-A2"


def test_retracted_replacement_reinstates_old_part_and_fault():
    c = controller()
    weld_charge(c)
    c.register_contactor_replacement("charge", "charge-A2", 2.0,
                                     note="registered by mistake")
    assert "charge_contactor_mismatch" not in c.active_reasons

    event = c.retract_contactor_replacement(
        "charge", 2.5, reason="wrong contactor logged"
    )
    assert event.event_type == "contactor_replacement_retracted"
    assert event.details["reinstated_part_id"] == "charge-original"
    assert "charge_contactor_mismatch" in event.details[
        "recovery_conditions"
    ]

    # The original replacement event stays in the log as a fact.
    assert events(c, "contactor_replaced")
    assert c.current_parts["charge"] == "charge-original"
    # The old welded fault applies to the reinstalled old part again.
    assert "charge_contactor_mismatch" in c.active_reasons
    blocked = c.request_reset(2.5)
    assert "charge_contactor_mismatch" in blocked[0].details[
        "blocked_reasons"
    ]
    log = c.replacement_log
    assert [entry["kind"] for entry in log] == ["replaced", "retracted"]
    assert log[0]["retracted"] is True


def test_replacement_survives_restart_and_replay(tmp_path):
    path = tmp_path / "replacement.jsonl"
    recorder = JsonlEventRecorder(path)
    c = controller()
    first = sample(0.0, charge_feedback=True, discharge_feedback=True,
                   load_voltage=48.0, value=3.7)
    recorder.record_measurement(first)
    recorder.record_events(c.update(first))
    second = sample(1.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0)
    recorder.record_measurement(second)
    recorder.record_events(c.update(second))
    third = sample(1.2, charge_feedback=True, discharge_feedback=True,
                   load_voltage=48.0)
    recorder.record_measurement(third)
    recorder.record_events(c.update(third))
    fourth = sample(1.8, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0)
    recorder.record_measurement(fourth)
    recorder.record_events(c.update(fourth))
    recorder.record_replacement(c, "charge", "charge-A2", 2.0,
                                note="welded pole swapped")

    # Restart mid-verification: the new part identity and the archived old
    # fault are restored, and the wait continues along the same stage.
    rebooted = restore_controller(path, config=config())
    assert rebooted.current_parts["charge"] == "charge-A2"
    assert rebooted.part_history[0]["faults"] == [
        "charge_contactor_mismatch"
    ]
    assert "post_disconnect_voltage_verification" in rebooted.active_reasons

    measurement = sample(2.2, charge_feedback=False, discharge_feedback=False,
                         load_voltage=4.0, value=4.05)
    recorder.record_measurement(measurement)
    recorder.record_events(rebooted.update(measurement))
    recorder.record_controller_state(rebooted)
    measurement = sample(2.8, charge_feedback=False, discharge_feedback=False,
                         load_voltage=4.0, value=4.05)
    recorder.record_measurement(measurement)
    recorder.record_events(rebooted.update(measurement))
    recorder.record_controller_state(rebooted)
    reset_events = recorder.record_reset(rebooted, 2.8)
    assert reset_events[0].event_type == "manual_reset"
    assert reset_events[0].details["charge_part_id"] == "charge-A2"

    trace = replay_recording(path, config=config())
    assert trace[-1]["current_parts"]["charge"] == "charge-A2"


def test_retraction_replays_from_recording(tmp_path):
    path = tmp_path / "retraction.jsonl"
    recorder = JsonlEventRecorder(path)
    c = controller()
    for measurement in (
        sample(0.0, charge_feedback=True, discharge_feedback=True,
               load_voltage=48.0, value=3.7),
        sample(1.0, charge_feedback=True, discharge_feedback=True,
               load_voltage=48.0),
        sample(1.2, charge_feedback=True, discharge_feedback=True,
               load_voltage=48.0),
        sample(1.8, charge_feedback=True, discharge_feedback=True,
               load_voltage=48.0),
    ):
        recorder.record_measurement(measurement)
        recorder.record_events(c.update(measurement))
    assert "charge_contactor_mismatch" in c.active_reasons
    recorder.record_replacement(c, "charge", "charge-A2", 2.0,
                                note="registered by mistake")
    recorder.record_retraction(c, "charge", 2.5,
                               reason="wrong contactor logged")
    assert "charge_contactor_mismatch" in c.active_reasons

    rebooted = restore_controller(path, config=config())
    assert rebooted.current_parts["charge"] == "charge-original"
    assert "charge_contactor_mismatch" in rebooted.active_reasons
    trace = replay_recording(path, config=config())
    assert trace[-1]["current_parts"]["charge"] == "charge-original"
