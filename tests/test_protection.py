import pytest

from battery_platform.models import Measurement
from battery_platform.protection import FaultRule, ProtectionConfig, ProtectionController


def sample(timestamp: float, value: float = 3.7, temperature: float = 30.0):
    return Measurement(
        timestamp,
        [value, 3.7, 3.7, 3.7],
        [temperature, 30.0, 30.0, 30.0],
    )


def event_types(controller):
    return [event.event_type for event in controller.replay()]


def test_short_overvoltage_spike_does_not_open_contactor():
    controller = ProtectionController()

    assert controller.update(sample(0.0))
    assert controller.update(sample(0.5, value=4.25)) == []
    assert controller.update(sample(1.0, value=4.0)) == []

    assert controller.charge_contactor_closed is True
    assert "protection_triggered" not in event_types(controller)


def test_overvoltage_triggers_then_stays_locked_until_manual_reset():
    controller = ProtectionController()
    controller.update(sample(0.0))

    assert controller.update(sample(1.0, value=4.2)) == []
    trigger = controller.update(sample(2.0, value=4.25))
    assert trigger[0].event_type == "protection_triggered"
    assert trigger[0].reasons == ("over_voltage",)
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is True

    assert controller.update(sample(3.0, value=4.05)) == []
    blocked = controller.request_reset(3.5)
    assert blocked[0].event_type == "reset_blocked"

    ready = controller.update(sample(4.0, value=4.05))
    assert ready[0].event_type == "recovery_ready_manual_reset_required"
    assert controller.charge_contactor_closed is False

    reset = controller.request_reset(4.0)
    assert reset[0].event_type == "manual_reset"
    assert controller.charge_contactor_closed is True


def test_overvoltage_reset_remains_blocked_above_recovery_threshold():
    controller = ProtectionController()
    controller.update(sample(0.0))
    controller.update(sample(1.0, value=4.21))
    controller.update(sample(2.0, value=4.21))

    assert controller.charge_contactor_closed is False
    assert controller.update(sample(3.0, value=4.15)) == []

    reset = controller.request_reset(3.0)

    assert reset[0].event_type == "reset_blocked"
    assert reset[0].reasons == ()
    assert reset[0].details["blocked_reasons"] == ["over_voltage"]
    assert controller.charge_contactor_closed is False
    assert "over_voltage" in controller.reset_blocked_reasons


def test_recovery_timer_restarts_when_boundary_is_recrossed():
    controller = ProtectionController()
    controller.update(sample(0.0))
    controller.update(sample(1.0, value=4.21))
    controller.update(sample(2.0, value=4.21))

    assert controller.update(sample(2.5, value=4.05)) == []
    assert controller.update(sample(3.0, value=4.15)) == []
    blocked = controller.request_reset(3.0)
    assert blocked[0].event_type == "reset_blocked"

    assert controller.update(sample(3.5, value=4.05)) == []
    assert controller.update(sample(4.0, value=4.05)) == []
    blocked_again = controller.request_reset(4.0)
    assert blocked_again[0].event_type == "reset_blocked"

    ready = controller.update(sample(4.5, value=4.05))
    assert ready[0].event_type == "recovery_ready_manual_reset_required"
    reset = controller.request_reset(4.5)
    assert reset[0].event_type == "manual_reset"
    assert controller.charge_contactor_closed is True


def test_concurrent_latched_protections_keep_independent_reasons():
    controller = ProtectionController(
        ProtectionConfig(
            over_temperature=FaultRule(55, 50, 0.0, 0.0),
        )
    )
    controller.update(sample(0.0))
    controller.update(sample(1.0, value=4.21, temperature=56))
    controller.update(sample(2.0, value=4.21, temperature=56))

    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is False
    assert set(controller.active_reasons) == {
        "over_voltage",
        "over_temperature",
        "post_disconnect_voltage_verification",
    }

    controller.update(sample(3.0, value=4.05, temperature=49))
    partial_reset = controller.request_reset(3.0)

    assert partial_reset[0].event_type == "manual_reset"
    assert partial_reset[0].reasons == ("over_temperature",)
    assert set(partial_reset[0].details["blocked_reasons"]) == {
        "over_voltage",
        "post_disconnect_voltage_verification",
    }
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is False
    assert set(controller.active_reasons) == {
        "over_voltage",
        "post_disconnect_voltage_verification",
    }

    controller.update(sample(4.0, value=4.05, temperature=49))
    second_reset = controller.request_reset(4.0)
    assert second_reset[0].event_type == "manual_reset"
    assert second_reset[0].reasons == ("over_voltage",)
    assert second_reset[0].details["blocked_reasons"] == [
        "post_disconnect_voltage_verification"
    ]
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is False


def test_boundary_jitter_does_not_repeat_switching():
    controller = ProtectionController()
    for index in range(12):
        value = 4.21 if index % 2 else 4.09
        controller.update(sample(index * 0.2, value=value))

    triggers = [
        event
        for event in controller.replay()
        if event.event_type == "protection_triggered"
    ]
    assert triggers == []


def test_undervoltage_opens_discharge_after_configured_duration():
    config = ProtectionConfig(
        under_voltage=FaultRule(
            trigger_threshold=3.0,
            recovery_threshold=3.1,
            trigger_delay=0.4,
            recovery_delay=0.2,
        )
    )
    controller = ProtectionController(config)
    controller.update(sample(0.0))
    controller.update(sample(0.2, value=2.95))
    events = controller.update(sample(0.4, value=2.95))

    assert events[0].event_type == "protection_triggered"
    assert controller.discharge_contactor_closed is False
    assert controller.charge_contactor_closed is True


def test_overtemperature_opens_both_contactors():
    config = ProtectionConfig(
        over_temperature=FaultRule(55, 50, 0.0, 0.0)
    )
    controller = ProtectionController(config)

    events = controller.update(sample(0.0, temperature=55))

    assert events[0].event_type == "protection_triggered"
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is False


def test_missing_measurement_is_not_treated_as_normal():
    controller = ProtectionController()
    controller.update(sample(0.0))
    measurement = Measurement(1.0, [None, 3.7, 3.7, 3.7], [30.0] * 4)

    events = controller.update(measurement)

    assert events[0].event_type == "protection_triggered"
    assert events[0].reasons == ("measurement_fault",)
    assert controller.charge_contactor_closed is False
    assert controller.discharge_contactor_closed is False


def test_replay_records_actions_and_recovery_conditions():
    controller = ProtectionController(
        ProtectionConfig(
            over_temperature=FaultRule(55, 50, 0.0, 0.0)
        )
    )
    controller.update(sample(0.0, temperature=56))

    trigger = next(
        event
        for event in controller.replay()
        if event.event_type == "protection_triggered"
    )
    assert trigger.details["action"] == "open_both_contactors"
    assert trigger.details["recovery_threshold"] == 50
    assert trigger.details["recovery_delay"] == 0.0


pytest.fixture
def no_fixture_needed():
    return None
