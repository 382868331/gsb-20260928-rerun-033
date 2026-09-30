from battery_platform.models import ContactorFeedback, Measurement
from battery_platform.protection import (
    ContactorFeedbackConfig,
    ProtectionConfig,
    ProtectionController,
)


def config():
    return ProtectionConfig(
        contactor_feedback=ContactorFeedbackConfig(
            mismatch_delay=1.0,
            recovery_delay=1.0,
        )
    )


def sample(timestamp, voltage=3.7):
    return Measurement(timestamp, [voltage] * 4, [30.0] * 4)


def feedback(timestamp, charge, discharge):
    return ContactorFeedback(timestamp, charge, discharge)


def start(controller):
    controller.update(sample(0.0))
    controller.update_feedback(feedback(0.0, False, False))


def types(controller):
    return [event.event_type for event in controller.replay()]


def open_charge(controller, t0):
    controller.update(sample(t0, 4.25))
    controller.update(sample(t0 + 1.0, 4.25))


def test_command_and_observed_state_are_recorded_separately():
    controller = ProtectionController(config())
    start(controller)

    controller.update_feedback(feedback(0.2, True, True))

    assert controller.charge_contactor_closed is True
    assert controller.observed_charge_contactor_closed() is True

    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.5, True, True))

    assert controller.charge_contactor_closed is False
    assert controller.observed_charge_contactor_closed() is True
    assert "contactor_feedback_fault" not in types(controller)


def test_feedback_arriving_late_within_delay_does_not_fault():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))

    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.2, True, True))
    controller.update_feedback(feedback(2.9, False, True))
    controller.update_feedback(feedback(4.0, False, True))

    assert "contactor_feedback_fault" not in types(controller)
    assert controller.active_reasons == ("over_voltage",)


def test_stuck_feedback_confirms_fault_after_mismatch_delay():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))

    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.0, True, True))
    events = controller.update_feedback(feedback(3.0, True, True))

    assert events[0].event_type == "contactor_feedback_fault"
    assert events[0].reasons == ("charge_contactor_feedback",)
    assert events[0].details["commanded_closed"] is False
    assert events[0].details["observed_closed"] is True
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is True


def test_brief_physical_recovery_restarts_recovery_wait():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))
    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.0, True, True))
    controller.update_feedback(feedback(3.0, True, True))

    controller.update_feedback(feedback(3.5, False, True))
    controller.update_feedback(feedback(3.9, True, True))
    controller.update_feedback(feedback(4.5, False, True))
    ready = controller.update_feedback(feedback(5.5, False, True))

    assert ready[0].event_type == "recovery_ready_manual_reset_required"
    assert "contactor_feedback_fault" in types(controller)
    assert controller.charge_contactor_closed is False


def test_reissuing_same_open_command_does_not_shorten_wait():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))
    open_charge(controller, 1.0)

    controller.update_feedback(feedback(2.0, True, True))
    # OV recovery briefly makes the commanded side change, then OV re-triggers.
    controller.update(sample(2.4, 4.05))
    controller.update_feedback(feedback(2.4, False, True))
    controller.update(sample(2.6, 4.25))
    controller.update(sample(3.6, 4.25))
    controller.update_feedback(feedback(3.6, True, True))
    events = controller.update_feedback(feedback(4.6, True, True))

    assert events[0].event_type == "contactor_feedback_fault"
    assert controller.active_reasons == ("over_voltage", "charge_contactor_feedback")


def test_command_reopen_restarts_full_mismatch_wait():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))
    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.0, True, True))
    controller.update_feedback(feedback(2.5, False, True))

    controller.update(sample(2.4, 4.05))
    controller.update(sample(3.4, 4.05))
    controller.update_feedback(feedback(3.5, False, True))
    reset = controller.request_reset(3.5)
    assert reset[0].reasons == ("over_voltage",)
    assert controller.charge_contactor_closed is True
    assert "contactor_feedback_fault" not in types(controller)

    controller.update(sample(3.8, 4.25))
    controller.update(sample(4.8, 4.25))
    stuck = controller.update_feedback(feedback(4.8, True, True))
    assert stuck[0].event_type == "contactor_state"
    assert controller.update_feedback(feedback(5.5, True, True))[0].event_type == (
        "contactor_state"
    )
    events = controller.update_feedback(feedback(5.9, True, True))
    assert events[0].event_type == "contactor_feedback_fault"


def test_other_protection_reset_does_not_release_contact_fault():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))
    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.0, True, True))
    controller.update_feedback(feedback(3.0, True, True))

    controller.update(sample(3.5, 4.05))
    controller.update_feedback(feedback(3.5, False, True))
    controller.update(sample(4.5, 4.05))
    controller.update_feedback(feedback(4.5, False, True))
    reset = controller.request_reset(4.5)

    assert reset[0].event_type == "manual_reset"
    assert set(reset[0].reasons) == {
        "over_voltage",
        "charge_contactor_feedback",
    }


def test_manual_reset_requires_sustained_match():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))
    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.0, True, True))
    controller.update_feedback(feedback(3.0, True, True))

    blocked = controller.request_reset(3.0)
    assert blocked[0].event_type == "reset_blocked"
    assert "charge_contactor_feedback" in blocked[0].details["blocked_reasons"]


def test_repower_requires_open_command_and_open_feedback():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))
    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.0, True, True))
    controller.update_feedback(feedback(3.0, True, True))

    controller.update_feedback(feedback(3.5, False, True))
    controller.update_feedback(feedback(4.5, False, True))
    first_reset = controller.request_reset(4.5)
    assert first_reset[0].reasons == ("charge_contactor_feedback",)

    controller.update(sample(4.6, 4.05))
    controller.update_feedback(feedback(4.6, False, True))
    controller.update(sample(5.6, 4.05))
    controller.update_feedback(feedback(5.6, False, True))
    second_reset = controller.request_reset(5.6)
    assert second_reset[0].reasons == ("over_voltage",)

    assert controller.repower(6.0)[0].event_type == "repower_blocked"
    controller.update_feedback(feedback(6.1, False, False))
    ready = controller.repower(6.2)
    assert ready[0].event_type == "power_on_ready"
    assert controller.power_on_ready is True


def test_missing_feedback_is_treated_as_mismatch_not_as_success():
    controller = ProtectionController(config())
    start(controller)
    controller.update_feedback(feedback(0.2, True, True))
    open_charge(controller, 1.0)
    controller.update_feedback(feedback(2.0, None, True))
    events = controller.update_feedback(feedback(3.0, None, True))

    assert events[0].event_type == "contactor_feedback_fault"
    assert controller.charge_contactor_closed is False
