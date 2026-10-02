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
    value=4.21,
    charge_feedback=None,
    discharge_feedback=None,
    load_voltage=4.0,
    charge_component=None,
    discharge_component=None,
):
    return Measurement(
        timestamp,
        [value, 3.7, 3.7, 3.7],
        [30.0] * 4,
        charge_contactor_feedback=charge_feedback,
        discharge_contactor_feedback=discharge_feedback,
        load_voltage=load_voltage,
        charge_contactor_component_id=charge_component,
        discharge_contactor_component_id=discharge_component,
    )


def controller():
    return ProtectionController(
        ProtectionConfig(
            over_voltage=FaultRule(4.2, 4.1, 0.0, 0.0, locked=False),
            contactor_mismatch=FaultRule(None, None, 0.5, 0.5),
            post_disconnect_voltage=FaultRule(5.0, 5.0, 0.0, 0.5),
        )
    )


def establish_both_welded(c):
    c.update(sample(0.0, value=3.7, charge_feedback=True, discharge_feedback=True))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True))
    c.update(sample(1.6, charge_feedback=True, discharge_feedback=True))
    c.update(sample(2.2, charge_feedback=True, discharge_feedback=True))
    c.update(sample(2.8, charge_feedback=True, discharge_feedback=True))
    assert {
        "charge_contactor_mismatch",
        "discharge_contactor_mismatch",
    } <= set(c.active_reasons)
    c.update(sample(3.4, value=4.05, charge_feedback=True, discharge_feedback=True))


def test_single_replacement_keeps_old_fault_and_other_pole_blocked():
    c = controller()
    establish_both_welded(c)

    registered = c.register_contactor_replacement("charge", "charge-new-1", 3.0)

    assert registered[0].event_type == "contactor_replacement_registered"
    assert "charge_contactor_mismatch" not in c.active_reasons
    assert "discharge_contactor_mismatch" in c.active_reasons
    assert c.charge_contactor_closed is False
    assert c.discharge_contactor_closed is False

    records = c.contactor_components["charge"]["components"]
    assert records[0]["component_id"] == "charge:original"
    assert records[0]["fault_locked"] is True
    assert records[0]["status"] == "replaced"
    assert records[1]["component_id"] == "charge-new-1"
    assert records[1]["fault_locked"] is False

    c.update(
        sample(
            3.6,
            value=4.05,
            charge_feedback=False,
            discharge_feedback=True,
            load_voltage=4.0,
        )
    )
    reset = c.request_reset(3.6)
    assert reset[0].event_type == "reset_blocked"
    assert "discharge_contactor_mismatch" in reset[0].details["blocked_reasons"]
    assert c.charge_contactor_closed is False
    assert c.discharge_contactor_closed is False


def test_two_replacements_require_new_feedback_and_sustained_voltage_before_reset():
    c = controller()
    establish_both_welded(c)

    c.register_contactor_replacement("charge", "charge-new-1", 3.0)
    c.register_contactor_replacement("discharge", "discharge-new-1", 3.2)

    c.update(
        sample(
            3.4,
            value=4.05,
            charge_feedback=False,
            discharge_feedback=False,
            load_voltage=48.0,
        )
    )
    assert c.request_reset(3.4)[0].event_type == "reset_blocked"

    c.update(
        sample(
            3.6,
            value=4.05,
            charge_feedback=False,
            discharge_feedback=False,
            load_voltage=4.0,
        )
    )
    c.update(
        sample(
            4.2,
            value=4.05,
            charge_feedback=False,
            discharge_feedback=False,
            load_voltage=48.0,
        )
    )
    assert c.request_reset(4.2)[0].event_type == "reset_blocked"

    c.update(
        sample(
            4.4,
            value=4.05,
            charge_feedback=False,
            discharge_feedback=False,
            load_voltage=4.0,
        )
    )
    c.update(
        sample(
            5.0,
            value=4.05,
            charge_feedback=False,
            discharge_feedback=False,
            load_voltage=4.0,
        )
    )
    reset = c.request_reset(5.0)
    assert reset[0].event_type == "manual_reset"
    assert reset[0].reasons == ("post_disconnect_voltage_verification",)
    assert c.charge_contactor_closed is True
    assert c.discharge_contactor_closed is True

    for name in ("charge", "discharge"):
        records = c.contactor_components[name]["components"]
        assert records[0]["fault_locked"] is True
        assert records[1]["fault_locked"] is False


def test_late_feedback_from_old_component_is_historical_evidence_only():
    c = controller()
    establish_both_welded(c)
    c.register_contactor_replacement("charge", "charge-new-1", 3.0)

    stale = c.update(
        sample(
            3.6,
            value=4.05,
            charge_feedback=True,
            discharge_feedback=True,
            charge_component="charge:original",
        )
    )
    assert stale[0].event_type == "stale_contactor_feedback_ignored"
    assert stale[0].details["observed_component_id"] == "charge:original"
    assert c.charge_contactor_observed_closed is None
    assert "charge_contactor_mismatch" not in c.active_reasons

    records = c.contactor_components["charge"]["components"]
    assert records[0]["status"] == "historical_feedback_received"
    assert records[0]["fault_locked"] is True


def test_withdraw_mistaken_replacement_restores_old_component_state():
    c = controller()
    establish_both_welded(c)
    c.register_contactor_replacement("charge", "wrong-note-1", 3.0)

    withdrawn = c.withdraw_contactor_replacement("charge", 3.2)

    assert withdrawn[0].event_type == "contactor_replacement_withdrawn"
    assert withdrawn[0].details["issued_commands_remain_historical_facts"] is True
    assert "charge_contactor_mismatch" in c.active_reasons
    assert c.contactor_components["charge"]["current_component_id"] == "charge:original"
    records = c.contactor_components["charge"]["components"]
    assert [record["component_id"] for record in records] == [
        "charge:original",
        "wrong-note-1",
    ]
    assert records[0]["status"] == "installed"
    assert records[1]["status"] == "withdrawn"
    event_types = [event.event_type for event in c.replay()]
    assert "contactor_replacement_registered" in event_types
    assert "contactor_replacement_withdrawn" in event_types


def test_replacement_state_and_history_restore_after_restart(tmp_path):
    path = tmp_path / "replacement.jsonl"
    recorder = JsonlEventRecorder(path)
    c = controller()
    establish_both_welded(c)
    recorder.record_events(
        c.register_contactor_replacement("charge", "charge-new-1", 3.0)
    )
    recorder.record_controller_state(c)

    rebooted = restore_controller(path)
    assert "charge_contactor_mismatch" not in rebooted.active_reasons
    assert "discharge_contactor_mismatch" in rebooted.active_reasons
    old_record = rebooted.contactor_components["charge"]["components"][0]
    assert old_record["component_id"] == "charge:original"
    assert old_record["fault_locked"] is True
    assert rebooted.contactor_components["charge"]["current_component_id"] == "charge-new-1"


def test_replacement_and_withdrawal_are_replayable(tmp_path):
    path = tmp_path / "history.jsonl"
    recorder = JsonlEventRecorder(path)
    c = controller()
    timeline = [
        sample(0.0, value=3.7, charge_feedback=True, discharge_feedback=True),
        sample(1.0, charge_feedback=True, discharge_feedback=True),
        sample(1.6, charge_feedback=True, discharge_feedback=True),
        sample(2.2, charge_feedback=True, discharge_feedback=True),
        sample(2.8, charge_feedback=True, discharge_feedback=True),
        sample(3.4, value=4.05, charge_feedback=True, discharge_feedback=True),
    ]
    for measurement in timeline:
        recorder.record_measurement(measurement)
        recorder.record_events(c.update(measurement))
    recorder.record_contactor_replacement(c, "charge", "charge-new-1", 4.0)
    recorder.record_contactor_replacement_withdrawal(c, "charge", 4.2)
    later = sample(
        4.4,
        value=4.05,
        charge_feedback=True,
        discharge_feedback=True,
    )
    recorder.record_measurement(later)
    recorder.record_events(c.update(later))

    trace = replay_recording(path, c.config)
    assert trace[-1]["charge_contactor_component_id"] == "charge:original"
    assert trace[-1]["discharge_contactor_component_id"] == "discharge:original"
    assert "charge_contactor_mismatch" in trace[-1]["active_reasons"]
    assert "discharge_contactor_mismatch" in trace[-1]["active_reasons"]
