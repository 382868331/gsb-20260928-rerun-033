from battery_platform.protection import ProtectionController
from battery_platform.recorder import (
    JsonlEventRecorder,
    replay_recording,
    restore_controller,
)

from test_contactor_replacement import config, controller, events, sample, weld_charge


def weld_both(c):
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0, value=3.7))
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    c.update(sample(1.6, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    c.update(sample(2.2, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    c.update(sample(2.8, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    assert "charge_contactor_mismatch" in c.active_reasons
    assert "discharge_contactor_mismatch" in c.active_reasons


def test_feedback_attribution_isolated_by_part_generation():
    c = controller()
    weld_both(c)

    # Only the charge pole is replaced; a new generation starts there.
    c.register_contactor_replacement("charge", "charge-A2", 3.0)
    assert c.current_parts == {
        "charge": "charge-A2",
        "discharge": "discharge-original",
    }
    assert c.part_generations == {"charge": 1, "discharge": 0}
    assert c.part_history[0]["generation"] == 0

    # Late close feedback from the retired charge part only enters history.
    late = c.submit_contactor_feedback(
        "charge", True, part_id="charge-original", timestamp=3.1
    )
    assert late[0].event_type == "late_contactor_feedback"
    assert late[0].details["attributed_to"] == "part_history"
    assert late[0].details["current_part_id"] == "charge-A2"
    history = c.part_history
    assert history[0]["late_feedback"][-1]["observed_closed"] is True
    assert history[0]["late_feedback"][-1]["current_generation"] == 1
    # Live state is untouched: the welded observation stays and no new
    # mismatch timing starts for the new part.
    assert c.charge_contactor_observed_closed is True
    assert "charge_contactor_mismatch" not in c.active_reasons

    # A late "open" report from the old part must not satisfy verification.
    c.submit_contactor_feedback(
        "charge", False, part_id="charge-original", timestamp=3.15
    )
    assert c.charge_contactor_observed_closed is True
    assert not events(c, "post_disconnect_verification_passed")

    # The new part's own normal feedback is attributed to the live state.
    attributed = c.submit_contactor_feedback(
        "charge", False, part_id="charge-A2", timestamp=3.2
    )
    assert attributed[0].event_type == "contactor_feedback_attributed"
    assert attributed[0].details["generation"] == 1
    assert c.charge_contactor_observed_closed is False

    # The other old part is still faulty; one normal-looking sample from it
    # is not enough to clear its locked mismatch.
    c.submit_contactor_feedback(
        "discharge", False, part_id="discharge-original", timestamp=3.3
    )
    assert "discharge_contactor_mismatch" in c.active_reasons

    # Reset must satisfy every installed part: the welded discharge pole and
    # the unfinished disconnect verification both block it.
    c.update(sample(3.6, charge_feedback=False, discharge_feedback=True,
                    load_voltage=4.0, value=4.05))
    blocked = c.request_reset(3.6)
    blocked_reasons = blocked[0].details["blocked_reasons"]
    assert "discharge_contactor_mismatch" in blocked_reasons
    assert "post_disconnect_voltage_verification" in blocked_reasons
    assert "charge_contactor_mismatch" not in blocked[0].details[
        "reset_reasons"
    ]
    assert blocked[0].details["charge_part_id"] == "charge-A2"

    # Retracting the replacement reinstalls the old part and its fault.
    retracted = c.retract_contactor_replacement(
        "charge", 4.0, reason="wrong position logged"
    )
    assert retracted.details["reinstated_part_id"] == "charge-original"
    assert "charge_contactor_mismatch" in c.active_reasons
    assert c.part_generations == {"charge": 0, "discharge": 0}
    assert c.replacement_log[0]["retracted"] is True


def test_unattributable_feedback_stays_pending_with_reason():
    c = controller()
    weld_charge(c)

    missing = c.submit_contactor_feedback("charge", False, timestamp=2.0)
    assert missing[0].event_type == "contactor_feedback_pending"
    assert missing[0].details["pending_reason"] == "missing_part_id"

    unknown = c.submit_contactor_feedback(
        "charge", False, part_id="charge-ghost", timestamp=2.1
    )
    assert unknown[0].details["pending_reason"] == "unknown_part_id"

    pending = c.pending_feedback
    assert [entry["reason"] for entry in pending] == [
        "missing_part_id",
        "unknown_part_id",
    ]
    assert pending[0]["part_id"] is None
    assert pending[1]["part_id"] == "charge-ghost"
    # Pending feedback never touches live state.
    assert c.charge_contactor_observed_closed is True
    assert "charge_contactor_mismatch" in c.active_reasons


def test_attribution_and_timing_windows_survive_restart():
    c = controller()
    weld_charge(c)
    c.register_contactor_replacement("charge", "charge-A2", 2.0)
    c.submit_contactor_feedback(
        "charge", True, part_id="charge-original", timestamp=2.1
    )
    c.submit_contactor_feedback("charge", False, timestamp=2.1)

    # Mid-verification: low-voltage observation has started but not finished.
    c.update(sample(2.2, charge_feedback=False, discharge_feedback=False,
                    load_voltage=4.0, value=4.05))
    verification = c.post_disconnect_verification
    assert verification["low_voltage_since"] == 2.2
    assert not verification["recovery_ready"]

    snapshot = c.state_snapshot()
    rebooted = ProtectionController(config())
    rebooted.restore_state(snapshot)

    # Attribution data and timing windows are intact after the restart.
    assert rebooted.part_generations == {"charge": 1, "discharge": 0}
    assert rebooted.pending_feedback == c.pending_feedback
    assert rebooted.part_history[0]["late_feedback"][-1][
        "observed_closed"
    ] is True
    restored = rebooted.post_disconnect_verification
    assert restored["both_open_since"] == verification["both_open_since"]
    assert restored["low_voltage_since"] == 2.2

    # The wait continues along the same window; it is not restarted.
    rebooted.update(sample(2.8, charge_feedback=False,
                           discharge_feedback=False, load_voltage=4.0,
                           value=4.05))
    passed = events(rebooted, "post_disconnect_verification_passed")
    assert passed
    assert passed[0].details["charge_part_id"] == "charge-A2"
    reset = rebooted.request_reset(2.8)
    assert reset[0].event_type == "manual_reset"

    # Late feedback for the retired part still resolves to history after
    # the restart, not to the installed part.
    late = rebooted.submit_contactor_feedback(
        "charge", True, part_id="charge-original", timestamp=3.0
    )
    assert late[0].event_type == "late_contactor_feedback"
    assert rebooted.charge_contactor_observed_closed is False


def test_feedback_attribution_replays_from_recording(tmp_path):
    path = tmp_path / "feedback.jsonl"
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
    recorder.record_replacement(c, "charge", "charge-A2", 2.0)
    recorder.record_feedback(c, "charge", True, part_id="charge-original",
                             timestamp=2.1)
    recorder.record_feedback(c, "charge", False, timestamp=2.1)
    recorder.record_feedback(c, "charge", False, part_id="charge-A2",
                             timestamp=2.2)

    rebooted = restore_controller(path, config=config())
    assert rebooted.current_parts["charge"] == "charge-A2"
    assert rebooted.part_generations["charge"] == 1
    assert rebooted.part_history[0]["late_feedback"][-1][
        "observed_closed"
    ] is True
    assert [entry["reason"] for entry in rebooted.pending_feedback] == [
        "missing_part_id"
    ]
    assert rebooted.charge_contactor_observed_closed is False

    trace = replay_recording(path, config=config())
    assert trace[-1]["current_parts"]["charge"] == "charge-A2"
