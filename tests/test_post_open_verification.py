from battery_platform.experiment import ExperimentScenario, run_experiment
from battery_platform.models import Measurement
from battery_platform.protection import (
    FaultRule,
    ProtectionConfig,
    ProtectionController,
)
from battery_platform.recorder import JsonlEventRecorder, replay_recording


def sample(
    timestamp,
    value=3.7,
    temperature=30.0,
    charge_feedback=None,
    discharge_feedback=None,
    bus_voltage=None,
):
    return Measurement(
        timestamp,
        [value, 3.7, 3.7, 3.7],
        [temperature, 30.0, 30.0, 30.0],
        charge_contactor_feedback=charge_feedback,
        discharge_contactor_feedback=discharge_feedback,
        bus_voltage=bus_voltage,
    )


def controller(**overrides):
    rules = {"over_temperature": FaultRule(55.0, 50.0, 0.0, 0.5)}
    rules.update(overrides)
    config = ProtectionConfig(**rules)
    return ProtectionController(config)


def open_both(ctrl, start=1.0, bus=0.0):
    ctrl.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                       bus_voltage=14.8))
    return ctrl.update(sample(start, temperature=56.0,
                              charge_feedback=False, discharge_feedback=False,
                              bus_voltage=bus))


def test_verification_requires_sustained_voltage_decay():
    ctrl = controller()
    events = open_both(ctrl)
    assert [event.event_type for event in events] == [
        "protection_triggered",
        "contactor_open_confirmed",
        "contactor_open_confirmed",
        "verification_started",
        "contactor_state",
    ]
    assert ctrl.disconnect_completed_by == ("charge", "discharge")
    assert ctrl.repower_allowed is False

    ctrl.update(sample(1.5, temperature=56.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    events = ctrl.update(sample(2.0, temperature=56.0, charge_feedback=False,
                                discharge_feedback=False, bus_voltage=0.0))
    assert [event.event_type for event in events] == ["verification_passed"]
    assert events[0].details["observed_delay"] == 1.0
    assert ctrl.repower_allowed is True


def test_verification_reasons_kept_separate():
    ctrl = controller()
    events = open_both(ctrl, bus=14.8)
    pending = [e for e in events if e.event_type == "verification_pending"]
    assert pending[0].details["reason"] == "voltage_not_decayed"

    events = ctrl.update(sample(2.0, temperature=56.0, charge_feedback=False,
                                discharge_feedback=False, bus_voltage=None))
    assert events[-1].details["reason"] == "observation_missing"

    # Voltage decays, then briefly recovers: a third, distinct reason.
    ctrl.update(sample(2.5, temperature=56.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.1))
    events = ctrl.update(sample(3.0, temperature=56.0, charge_feedback=False,
                                discharge_feedback=False, bus_voltage=9.0))
    assert events[-1].details["reason"] == "brief_recovery"
    assert ctrl.verification_status["pending_reasons"] == [
        "voltage_not_decayed",
        "observation_missing",
        "brief_recovery",
    ]

    # Good observation resumes from the banked 0.5 s, not from zero.
    ctrl.update(sample(3.5, temperature=56.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    events = ctrl.update(sample(4.0, temperature=56.0, charge_feedback=False,
                                discharge_feedback=False, bus_voltage=0.0))
    passed = [e for e in events if e.event_type == "verification_passed"]
    assert passed
    assert passed[0].details["cleared_reasons"] == [
        "voltage_not_decayed",
        "observation_missing",
        "brief_recovery",
    ]
    assert ctrl.verification_status["pending_reasons"] == []


def test_manual_reset_cannot_skip_incomplete_verification():
    ctrl = controller()
    open_both(ctrl)
    ctrl.update(sample(1.5, temperature=49.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    events = ctrl.request_reset(1.5)
    assert events[0].event_type == "reset_blocked"
    assert "post_open_verification" in events[0].details["blocked_reasons"]
    assert events[0].details["verification_blocking"] is True
    assert ctrl.repower_allowed is False

    # Finish verification, recover, then the reset is allowed.
    ctrl.update(sample(2.0, temperature=49.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    assert ctrl.repower_allowed is True
    ctrl.update(sample(2.5, temperature=49.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    events = ctrl.request_reset(2.5)
    assert events[0].event_type == "manual_reset"
    assert events[0].details["verification"]["passed"] is True


def test_reclose_mid_verification_continues_original_wait():
    ctrl = controller(
        over_temperature=FaultRule(55.0, 50.0, 0.0, 0.0, locked=False),
    )
    open_both(ctrl)
    # 0.5 s of good observation banked before the mid-verification re-close.
    ctrl.update(sample(1.5, temperature=56.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    # Fault clears (unlocked): contactors re-close, verification pauses.
    ctrl.update(sample(2.0, temperature=49.0, charge_feedback=True,
                       discharge_feedback=True, bus_voltage=14.8))
    assert ctrl.charge_contactor_closed is True
    assert ctrl.verification_status["passed"] is False

    # Re-open: the wait resumes from the banked 0.5 s.
    events = ctrl.update(sample(3.0, temperature=56.0, charge_feedback=False,
                                discharge_feedback=False, bus_voltage=0.0))
    passed = [e for e in events if e.event_type == "verification_passed"]
    assert len(passed) == 1
    assert passed[0].details["observed_delay"] == 1.0


def test_stuck_pole_identified_and_not_cleared_by_healthy_partner():
    ctrl = controller()
    ctrl.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                       bus_voltage=14.8))
    ctrl.update(sample(1.0, temperature=56.0, charge_feedback=False,
                       discharge_feedback=True, bus_voltage=0.0))
    events = ctrl.update(sample(1.6, temperature=56.0, charge_feedback=False,
                                discharge_feedback=True, bus_voltage=0.0))
    triggered = [e for e in events if e.event_type == "protection_triggered"]
    assert triggered[0].reasons == ("discharge_contactor_mismatch",)
    # The healthy pole actually performed the safety disconnect.
    assert ctrl.disconnect_completed_by == ("charge",)

    # The other pole's normal feedback plus a manual reset of the thermal
    # fault must not clear the confirmed stuck fault.
    ctrl.update(sample(2.0, temperature=49.0, charge_feedback=False,
                       discharge_feedback=True, bus_voltage=0.0))
    ctrl.update(sample(2.5, temperature=49.0, charge_feedback=False,
                       discharge_feedback=True, bus_voltage=0.0))
    events = ctrl.request_reset(2.5)
    assert events[0].event_type == "manual_reset"
    assert events[0].details["reset_reasons"] == ["over_temperature"]
    assert "discharge_contactor_mismatch" in ctrl.active_reasons
    assert ctrl.discharge_contactor_closed is False

    # Only the stuck pole's own sustained healthy feedback unlocks it.
    ctrl.update(sample(3.0, temperature=49.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    ctrl.update(sample(4.0, temperature=49.0, charge_feedback=False,
                       discharge_feedback=False, bus_voltage=0.0))
    events = ctrl.request_reset(4.0)
    assert "discharge_contactor_mismatch" in events[0].details["reset_reasons"]
    assert ctrl.active_reasons == ()


def test_contradictory_feedback_is_distinct_from_single_stuck_pole():
    ctrl = controller()
    ctrl.update(sample(0.0, charge_feedback=True, discharge_feedback=True))
    # Both poles commanded closed, but they report opposite states.
    ctrl.update(sample(1.0, charge_feedback=True, discharge_feedback=False))
    events = ctrl.update(
        sample(1.6, charge_feedback=True, discharge_feedback=False)
    )
    reasons = {
        reason
        for event in events
        if event.event_type == "protection_triggered"
        for reason in event.reasons
    }
    assert "discharge_contactor_mismatch" in reasons
    assert "feedback_contradiction" in reasons
    assert "feedback_contradiction" in ctrl.active_reasons


def test_experiment_scenarios_are_comparable_along_the_record(tmp_path):
    fast_charge = run_experiment(
        ExperimentScenario(
            name="fast_charge",
            charge_actuation_delay=0.1,
            discharge_actuation_delay=0.4,
        )
    )
    stuck_discharge = run_experiment(
        ExperimentScenario(
            name="stuck_discharge",
            charge_actuation_delay=0.1,
            discharge_actuation_delay=0.4,
            stuck="discharge",
        ),
        recorder=JsonlEventRecorder(tmp_path / "stuck.jsonl"),
    )

    assert any(
        event.event_type == "verification_passed"
        for event in fast_charge.events
    )
    assert not any(
        event.event_type == "verification_passed"
        for event in stuck_discharge.events
    )
    final = stuck_discharge.trace[-1]
    assert "discharge_contactor_mismatch" in final["active_reasons"]
    assert final["disconnect_completed_by"] == ("charge",)
    # The record keeps command, feedback and voltage evidence per sample.
    assert {
        "charge_requested_closed",
        "discharge_requested_closed",
        "charge_feedback",
        "discharge_feedback",
        "bus_voltage",
    } <= set(final)

    trace = replay_recording(tmp_path / "stuck.jsonl")
    assert trace[-1]["disconnect_completed_by"] == ("charge",)
