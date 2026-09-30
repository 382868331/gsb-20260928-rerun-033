from battery_platform.models import Measurement
from battery_platform.protection import (
    FaultRule,
    ProtectionConfig,
    ProtectionController,
)
from battery_platform.simulator import VirtualContactor


def sample(
    timestamp,
    value=3.7,
    temperature=30.0,
    charge_feedback=None,
    discharge_feedback=None,
):
    return Measurement(
        timestamp,
        [value, 3.7, 3.7, 3.7],
        [temperature, 30.0, 30.0, 30.0],
        charge_contactor_feedback=charge_feedback,
            discharge_contactor_feedback=discharge_feedback,
    )


def mismatches(controller):
    return [
        event
        for event in controller.replay()
        if event.event_type == "protection_triggered"
        and event.reasons[0].endswith("_contactor_mismatch")
    ]


def _start(controller):
    controller.update(
        sample(0.0, charge_feedback=True, discharge_feedback=True)
    )


def _controller(mismatch=None):
    over_voltage = FaultRule(4.20, 4.10, 0.0, 1.0)
    if mismatch is None:
        return ProtectionController(
            ProtectionConfig(over_voltage=over_voltage)
        )
    return ProtectionController(
        ProtectionConfig(over_voltage=over_voltage, contactor_mismatch=mismatch)
    )


def test_late_feedback_within_delay_does_not_fault():
    controller = _controller()
    _start(controller)

    # Over-voltage at t=1 -> open-charge command issued.
    controller.update(sample(1.0, value=4.21))
    # Feedback still closed: mismatch pending, command must not count as done.
    controller.update(
        sample(1.2, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    assert controller.charge_contactor_closed is False
    assert controller.charge_contactor_observed_closed is True
    assert mismatches(controller) == []

    # Contact physically opens before the 0.5 s confirmation delay.
    events = controller.update(
        sample(1.4, value=4.21, charge_feedback=False, discharge_feedback=True)
    )
    assert events == []
    assert mismatches(controller) == []


def test_stuck_open_feedback_confirms_mismatch_and_latches():
    controller = _controller()
    _start(controller)
    controller.update(sample(1.0, value=4.21))
    controller.update(
        sample(1.2, value=4.21, charge_feedback=True, discharge_feedback=True)
    )

    events = controller.update(
        sample(1.8, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    assert events[0].event_type == "protection_triggered"
    assert events[0].reasons == ("charge_contactor_mismatch",)
    assert events[0].details["requested_closed"] is False
    assert events[0].details["observed_closed"] is True
    # Mismatch fault forces both poles open.
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is False
    assert "charge_contactor_mismatch" in controller.active_reasons

    # A reset while the contactor is still stuck is blocked.
    blocked = controller.request_reset(2.0)
    assert blocked[0].event_type == "reset_blocked"
    assert blocked[0].details["blocked_reasons"] == [
        "over_voltage",
        "charge_contactor_mismatch",
    ]


def test_brief_feedback_recovery_during_wait_restarts_timing():
    controller = _controller()
    _start(controller)
    controller.update(sample(1.0, value=4.21))
    controller.update(
        sample(1.3, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    # Feedback briefly matches the open command.
    controller.update(
        sample(1.6, value=4.21, charge_feedback=False, discharge_feedback=True)
    )
    # Then it jitters back to closed: confirmation timer restarts.
    controller.update(
        sample(1.9, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    assert mismatches(controller) == []
    events = controller.update(
        sample(2.5, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    assert events[0].reasons == ("charge_contactor_mismatch",)


def test_brief_recovery_after_fault_does_not_clear_latch():
    controller = _controller()
    _start(controller)
    controller.update(
        sample(1.0, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    events = controller.update(
        sample(1.6, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    assert events[0].reasons == ("charge_contactor_mismatch",)

    # Over-voltage clears and the stuck pole momentarily reports open.
    controller.update(
        sample(2.0, value=4.21, charge_feedback=True, discharge_feedback=False)
    )
    controller.update(
        sample(2.4, value=4.05, charge_feedback=False, discharge_feedback=False)
    )
    # Jitters back before the 1.0 s recovery delay: still locked.
    controller.update(
        sample(3.2, value=4.05, charge_feedback=True, discharge_feedback=False)
    )
    reset = controller.request_reset(3.2)
    assert reset[0].event_type == "reset_blocked"
    assert "charge_contactor_mismatch" in reset[0].details["blocked_reasons"]


def test_command_change_mid_wait_does_not_shorten_delay():
    controller = ProtectionController(
        ProtectionConfig(
            over_voltage=FaultRule(4.20, 4.10, 0.0, 0.0, locked=False)
        )
    )
    _start(controller)
    controller.update(
        sample(1.0, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    controller.update(
        sample(1.4, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    # Over-voltage recovers at t=1.6 -> command flips back to closed while
    # feedback is still (now correctly) open: mismatch pending in reverse.
    controller.update(
        sample(1.6, value=4.05, charge_feedback=False, discharge_feedback=True)
    )
    # 0.4 s into the old wait must not carry over.
    controller.update(
        sample(1.9, value=4.05, charge_feedback=False, discharge_feedback=True)
    )
    assert mismatches(controller) == []
    events = controller.update(
        sample(2.2, value=4.05, charge_feedback=False, discharge_feedback=True)
    )
    assert events[0].reasons == ("charge_contactor_mismatch",)


def test_independent_protections_are_not_released_by_another_reset(tmp_path=None):
    config = ProtectionConfig(
        over_voltage=FaultRule(4.20, 4.10, 0.0, 1.0),
        over_temperature=FaultRule(55, 50, 0.0, 0.0),
    )
    controller = ProtectionController(config)
    _start(controller)
    controller.update(
        sample(1.0, value=4.21, temperature=56, charge_feedback=False,
               discharge_feedback=False)
    )
    assert set(controller.active_reasons) == {
        "over_voltage",
        "over_temperature",
    }

    # Temperature recovers and resets; over-voltage must keep the charge pole
    # open even though the reset itself changed lock state.
    controller.update(
        sample(2.0, value=4.21, temperature=49, charge_feedback=False,
               discharge_feedback=False)
    )
    reset = controller.request_reset(2.0)
    assert reset[0].reasons == ("over_temperature",)
    assert controller.charge_contactor_closed is False
    assert "over_voltage" in controller.active_reasons


def test_mismatch_recovers_then_requires_manual_reset():
    controller = _controller(FaultRule(None, None, 0.2, 0.2))
    _start(controller)
    controller.update(sample(1.0, value=4.21))
    controller.update(
        sample(1.3, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    fault = controller.update(
        sample(1.5, value=4.21, charge_feedback=True, discharge_feedback=True)
    )
    assert fault[0].reasons == ("charge_contactor_mismatch",)

    # Voltage normal, feedback stays consistent with open for the recovery
    # delay, then reset closes everything.
    controller.update(
        sample(2.0, value=4.05, charge_feedback=False, discharge_feedback=False)
    )
    ready = controller.update(
        sample(2.3, value=4.05, charge_feedback=False, discharge_feedback=False)
    )
    assert ready[0].event_type == "recovery_ready_manual_reset_required"
    reset = controller.request_reset(2.3)
    assert reset[0].event_type == "manual_reset"


def test_virtual_contactor_models_delay_and_stuck():
    delayed = VirtualContactor(name="charge", actuation_delay=0.3)
    delayed.apply_command(False, 0.0)
    assert delayed.observe(0.2) is True
    assert delayed.observe(0.4) is False

    stuck = VirtualContactor(name="welded", stuck=True)
    stuck.apply_command(False, 0.0)
    assert stuck.observe(5.0) is True


def test_mismatch_state_restored_after_power_cycle(tmp_path):
    from battery_platform.recorder import JsonlEventRecorder, restore_controller

    path = tmp_path / "power.jsonl"
    recorder = JsonlEventRecorder(path)
    controller = _controller()
    controller.update(
        sample(0.0, charge_feedback=True, discharge_feedback=True)
    )
    first = sample(
        1.0, value=4.21, charge_feedback=True, discharge_feedback=True
    )
    recorder.record_measurement(first)
    recorder.record_events(controller.update(first))
    recorder.record_controller_state(controller)
    measurement = sample(
        1.6, value=4.21, charge_feedback=True, discharge_feedback=True
    )
    recorder.record_measurement(measurement)
    recorder.record_events(controller.update(measurement))
    recorder.record_controller_state(controller)

    rebooted = restore_controller(path)
    assert "charge_contactor_mismatch" in rebooted.active_reasons
    assert rebooted.charge_contactor_closed is False
    assert rebooted.charge_contactor_observed_closed is True
