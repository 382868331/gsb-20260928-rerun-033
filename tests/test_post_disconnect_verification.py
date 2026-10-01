from battery_platform.models import Measurement
from battery_platform.protection import (
    FaultRule,
    ProtectionConfig,
    ProtectionController,
)
from battery_platform.recorder import JsonlEventRecorder, restore_controller


def sample(
    timestamp,
    *,
    charge_feedback,
    discharge_feedback,
    load_voltage,
    value=4.21,
    temperature=56.0,
):
    return Measurement(
        timestamp,
        [value, 3.7, 3.7, 3.7],
        [temperature] * 4,
        charge_contactor_feedback=charge_feedback,
        discharge_contactor_feedback=discharge_feedback,
        load_voltage=load_voltage,
    )


def controller():
    return ProtectionController(
        ProtectionConfig(
            over_voltage=FaultRule(4.2, 4.1, 0.0, 0.0),
            over_temperature=FaultRule(55.0, 50.0, 0.0, 0.0),
            post_disconnect_voltage=FaultRule(5.0, 5.0, 0.0, 0.5),
        )
    )


def events(controller, event_type):
    return [
        event
        for event in controller.replay()
        if event.event_type == event_type
    ]


def test_sequential_feedback_and_sustained_low_voltage_verify_second_opener():
    c = controller()
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0, value=3.7, temperature=30.0))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0))

    c.update(sample(1.2, charge_feedback=False, discharge_feedback=True, load_voltage=48.0))
    assert "post_disconnect_voltage_verification" in c.reset_blocked_reasons

    c.update(sample(1.4, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))
    assert events(c, "post_disconnect_verification_started")[0].details[
        "actual_disconnect_contactor"
    ] is None
    assert c.request_reset(1.4)[0].event_type == "reset_blocked"

    c.update(sample(1.9, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))
    passed = events(c, "post_disconnect_verification_passed")
    assert passed
    assert passed[0].details["actual_disconnect_contactor"] == "discharge"
    assert "post_disconnect_voltage_verification" in c.reset_ready_reasons


def test_missing_and_recovered_voltage_keep_distinct_failure_reasons():
    c = controller()
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0, value=3.7, temperature=30.0))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0))

    c.update(sample(1.2, charge_feedback=False, discharge_feedback=False, load_voltage=48.0))
    failed = events(c, "post_disconnect_verification_failed")
    assert failed[-1].details["failure_reason"] == "open_feedback_contradicts_voltage"

    missing = c.update(sample(1.4, charge_feedback=False, discharge_feedback=False, load_voltage=None))
    assert missing[-1].details["failure_reason"] == "load_voltage_observation_missing"
    assert c.request_reset(1.4)[0].event_type == "reset_blocked"

    c.update(sample(1.6, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))
    recovered = c.update(sample(2.2, charge_feedback=False, discharge_feedback=False, load_voltage=48.0))
    assert recovered[-1].details["failure_reason"] == "voltage_recovered"
    assert c.request_reset(2.2)[0].event_type == "reset_blocked"


def test_reopened_wait_continues_and_welded_fault_survives_reset():
    c = controller()
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0, value=3.7, temperature=30.0))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0))
    c.update(sample(1.2, charge_feedback=True, discharge_feedback=False, load_voltage=48.0))
    welded = c.update(sample(1.6, charge_feedback=True, discharge_feedback=False, load_voltage=48.0))
    assert welded[0].reasons == ("charge_contactor_mismatch",)

    c.update(sample(1.8, charge_feedback=True, discharge_feedback=False, load_voltage=4.0))
    blocked = c.request_reset(1.8)
    assert "charge_contactor_mismatch" in blocked[0].details["blocked_reasons"]
    assert "post_disconnect_voltage_verification" in blocked[0].details["blocked_reasons"]
    assert "charge_contactor_mismatch" in c.active_reasons


def test_verification_state_restores_mid_wait(tmp_path):
    path = tmp_path / "verification.jsonl"
    recorder = JsonlEventRecorder(path)
    c = controller()
    first = sample(0.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0, value=3.7, temperature=30.0)
    recorder.record_measurement(first)
    recorder.record_events(c.update(first))
    second = sample(1.0, charge_feedback=False, discharge_feedback=False, load_voltage=4.0)
    recorder.record_measurement(second)
    recorder.record_events(c.update(second))
    recorder.record_controller_state(c)

    rebooted = restore_controller(path)
    assert rebooted.post_disconnect_verification["low_voltage_since"] == 1.0
    rebooted.update(sample(1.2, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))
    rebooted.update(sample(1.5, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))
    assert events(rebooted, "post_disconnect_verification_passed")


def test_verification_continues_after_command_is_briefly_reopened():
    c = controller()
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0, value=3.7, temperature=30.0))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True, load_voltage=48.0))
    c.update(sample(1.2, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))

    c.update(sample(1.4, charge_feedback=False, discharge_feedback=False, load_voltage=4.0, temperature=49.0))
    c.update(sample(1.5, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))
    c.update(sample(1.7, charge_feedback=False, discharge_feedback=False, load_voltage=4.0))

    passed = events(c, "post_disconnect_verification_passed")
    assert passed
    assert passed[0].details["low_voltage_since"] == 1.2
    assert passed[0].details["both_open_since"] == 1.2
