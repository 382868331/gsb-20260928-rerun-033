import pytest

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


def trip_overvoltage(c):
    """Open both contactors via overvoltage; feedback stays unknown."""
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0, value=3.7))
    c.update(sample(1.0, charge_feedback=None, discharge_feedback=None,
                    load_voltage=48.0))
    assert c.charge_contactor_closed is False


def test_pending_review_shows_reason_and_installations():
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)

    pending = c.pending_feedback
    assert len(pending) == 1
    assert pending[0]["reason"] == "missing_part_id"
    assert pending[0]["feedback_id"] == "pf-1"

    review = c.pending_feedback_review()
    assert review[0]["reason"] == "missing_part_id"
    assert review[0]["ambiguity"] == []
    assert review[0]["installations"] == [
        {
            "part_id": "charge-original",
            "generation": 1,
            "installed_at": None,
            "retired_at": None,
            "retired_reason": None,
            "current": True,
        }
    ]

    pending_event = events(c, "part_feedback_pending")[0]
    assert pending_event.details["feedback_id"] == "pf-1"
    assert pending_event.details["reason"] == "missing_part_id"


def test_attribution_to_current_generation_applies_live():
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    assert c.charge_contactor_observed_closed is None

    produced = c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07",
        "panel log matches install sheet", 2.0,
    )
    assert produced[0].event_type == "part_feedback_attributed"
    assert produced[0].details["applied_to_live"] is True
    assert produced[0].details["operator"] == "tech-07"
    assert produced[0].details["pending_reason"] == "missing_part_id"
    assert c.charge_contactor_observed_closed is False
    assert c.pending_feedback == []

    log = c.attribution_log
    assert len(log) == 1
    assert log[0]["action"] == "attributed"
    assert log[0]["operator"] == "tech-07"
    assert log[0]["reason"] == "panel log matches install sheet"
    assert log[0]["timestamp"] == 2.0

    # The original report is archived unchanged.
    resolved = c.resolved_feedback
    assert resolved[0]["feedback"]["part_id"] is None
    assert resolved[0]["feedback"]["observed_closed"] is False
    assert resolved[0]["feedback"]["reason"] == "missing_part_id"


def test_attribution_requires_known_installation():
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    with pytest.raises(ValueError):
        c.attribute_pending_feedback(
            "pf-1", "charge-original", 2, "tech-07", "wrong gen", 2.0
        )
    with pytest.raises(ValueError):
        c.attribute_pending_feedback(
            "pf-1", "never-installed", 1, "tech-07", "bad part", 2.0
        )
    with pytest.raises(ValueError):
        c.attribute_pending_feedback(
            "pf-1", "charge-original", 1, "", "no operator", 2.0
        )
    assert len(c.pending_feedback) == 1


def test_attribution_to_retired_generation_stays_out_of_verification():
    c = controller()
    trip_overvoltage(c)
    c.register_contactor_replacement("charge", "charge-A2", 2.0)
    c.register_part_feedback("charge", None, False, 2.5)

    produced = c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07",
        "report predates the swap", 3.0,
    )
    assert produced[0].details["applied_to_live"] is False
    # The historical report must not touch the live state of the new part.
    assert c.charge_contactor_observed_closed is None
    history = c.part_history
    late = history[0]["late_feedback"]
    assert late[0]["observed_closed"] is False
    assert late[0]["attributed_by"] == "tech-07"


def test_duplicate_attribution_not_counted_twice():
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07", "first", 2.0
    )
    produced = c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07", "first", 2.5
    )
    assert produced[0].event_type == "part_feedback_attribution_duplicate"
    assert produced[0].details["original_decision_id"] == "pa-1"
    attributed = [
        entry for entry in c.attribution_log
        if entry["action"] == "attributed"
    ]
    assert len(attributed) == 1
    assert len(c.resolved_feedback) == 1

    # A different target for the same feedback requires a revoke first.
    with pytest.raises(ValueError):
        c.attribute_pending_feedback(
            "pf-1", "charge-A2", 2, "tech-07", "conflicting", 3.0
        )


def test_revoke_and_reattribute_keeps_full_history():
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07", "initial guess", 2.0
    )
    assert c.charge_contactor_observed_closed is False

    produced = c.revoke_attribution(
        "pf-1", "tech-08", "wrong work order", 3.0
    )
    assert produced[0].event_type == "part_feedback_attribution_revoked"
    assert produced[0].details["revoked_decision_id"] == "pa-1"
    # Feedback returns to pending unchanged; live observation rolled back.
    assert len(c.pending_feedback) == 1
    assert c.pending_feedback[0]["part_id"] is None
    assert c.charge_contactor_observed_closed is None
    assert c.resolved_feedback == []

    log = c.attribution_log
    assert [entry["action"] for entry in log] == ["attributed", "revoked"]
    assert log[1]["operator"] == "tech-08"
    assert log[1]["reason"] == "wrong work order"
    assert log[1]["supersedes"] == "pa-1"

    # Re-attribution after revoke works and gets its own decision id.
    produced = c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-08",
        "confirmed against install sheet", 4.0,
    )
    assert produced[0].details["decision_id"] == "pa-3"
    assert c.charge_contactor_observed_closed is False

    # Revoking again without an active decision fails.
    c.revoke_attribution("pf-1", "tech-08", "second thoughts", 5.0)
    with pytest.raises(ValueError):
        c.revoke_attribution("pf-1", "tech-08", "already revoked", 6.0)


def test_ambiguity_flags_for_reinstalls_late_reports_and_retractions():
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    c.register_contactor_replacement("charge", "charge-A2", 2.0)
    c.register_part_feedback("charge", None, True, 2.5)
    c.retract_contactor_replacement("charge", 3.0, reason="logged by mistake")
    c.register_part_feedback("discharge", None, False, 3.5)

    review = {
        entry["feedback_id"]: entry for entry in c.pending_feedback_review()
    }
    first = review["pf-1"]
    assert "multiple_installations" in first["ambiguity"]
    assert "installation_changed_after_report" in first["ambiguity"]
    assert "replacement_retracted_after_report" in first["ambiguity"]
    second = review["pf-2"]
    assert "replacement_retracted_after_report" in second["ambiguity"]
    assert "installation_changed_after_report" not in second["ambiguity"]
    assert review["pf-3"]["ambiguity"] == []

    # Same physical part installed at two generations is called out.
    c.register_contactor_replacement("charge", "charge-A3", 4.0)
    c.register_contactor_replacement("charge", "charge-original", 5.0)
    c.register_part_feedback("charge", None, True, 5.5)
    review = {
        entry["feedback_id"]: entry for entry in c.pending_feedback_review()
    }
    assert "part_installed_multiple_times" in review["pf-4"]["ambiguity"]


def test_snapshot_restore_keeps_pending_attribution_and_faults():
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    c.register_part_feedback("discharge", None, False, 1.6)
    c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07", "confirmed", 2.0
    )
    c.revoke_attribution("pf-1", "tech-08", "misread sheet", 3.0)

    snapshot = c.state_snapshot()
    restored = controller()
    restored.restore_state(snapshot)
    assert restored.pending_feedback == c.pending_feedback
    assert restored.attribution_log == c.attribution_log
    assert restored.resolved_feedback == c.resolved_feedback
    assert restored.active_reasons == c.active_reasons
    assert restored.reset_blocked_reasons == c.reset_blocked_reasons

    # The restored controller continues the same decision id sequence.
    restored.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-08", "re-checked", 4.0
    )
    assert restored.attribution_log[-1]["decision_id"] == "pa-3"
    new_pending = restored.register_part_feedback(
        "discharge", None, True, 4.5
    )
    assert new_pending[0].details["feedback_id"] == "pf-3"


def test_recorder_replay_roundtrip(tmp_path):
    path = tmp_path / "attribution.jsonl"
    recorder = JsonlEventRecorder(path)
    c = controller()
    for measurement in (
        sample(0.0, charge_feedback=True, discharge_feedback=True,
               load_voltage=48.0, value=3.7),
        sample(1.0, charge_feedback=None, discharge_feedback=None,
               load_voltage=48.0),
    ):
        recorder.record_measurement(measurement)
        recorder.record_events(c.update(measurement))
    recorder.record_part_feedback(c, "charge", None, False, 1.5)
    recorder.record_attribution(
        c, "pf-1", "charge-original", 1, "tech-07", "confirmed", 2.0
    )
    recorder.record_attribution_revoke(c, "pf-1", "tech-08", "misread", 3.0)
    recorder.record_attribution(
        c, "pf-1", "charge-original", 1, "tech-08", "re-checked", 4.0
    )

    trace = replay_recording(path, config())
    assert trace
    restored = restore_controller(path, config())
    assert restored.pending_feedback == []
    assert [
        entry["action"] for entry in restored.attribution_log
    ] == ["attributed", "revoked", "attributed"]
    assert restored.charge_contactor_observed_closed is False


def test_revoke_keeps_later_explicit_feedback():
    """A revocation only removes an explanation that still owns the state.

    Explicit feedback received after the attribution becomes the current
    protection basis; revoking the older manual decision must return the
    report to pending without rolling the contactor back.
    """
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07", "initial guess", 2.0
    )
    assert c.charge_contactor_observed_closed is False
    basis = c.observed_basis("charge")
    assert basis["kind"] == "attribution"
    assert basis["decision_id"] == "pa-1"

    # Real physical feedback arrives later for the installed part.
    c.register_part_feedback("charge", "charge-original", False, 2.5)
    assert c.observed_basis("charge")["kind"] == "part_feedback"

    produced = c.revoke_attribution(
        "pf-1", "tech-08", "wrong work order", 3.0
    )
    details = produced[0].details
    assert details["rolled_back_live_observation"] is False
    assert details["retained_basis"]["kind"] == "part_feedback"
    assert details["retained_basis"]["timestamp"] == 2.5
    # The later explicit feedback still owns the live observation.
    assert c.charge_contactor_observed_closed is False
    assert c.observed_basis("charge")["kind"] == "part_feedback"
    assert len(c.pending_feedback) == 1
    assert c.pending_feedback[0]["feedback_id"] == "pf-1"

    # Re-attribution must not overwrite the newer observation either.
    produced = c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-08",
        "confirmed against install sheet", 4.0,
    )
    assert c.charge_contactor_observed_closed is False
    # The re-attribution targets the same live part, so it becomes the
    # explanation of the current observation again.
    assert c.observed_basis("charge")["kind"] == "attribution"
    assert c.observed_basis("charge")["decision_id"] == "pa-3"


def test_revoke_after_replacement_does_not_touch_new_part():
    """Same numbered position, new generation: an old decision cannot roll
    the new part's observation back."""
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07", "initial guess", 2.0
    )
    assert c.observed_basis("charge")["generation"] == 1

    # Same position gets a new generation, then real feedback for it.
    c.register_contactor_replacement("charge", "charge-A2", 3.0)
    c.register_part_feedback("charge", "charge-A2", False, 3.5)
    assert c.observed_basis("charge")["generation"] == 2

    produced = c.revoke_attribution(
        "pf-1", "tech-08", "wrong work order", 4.0
    )
    details = produced[0].details
    assert details["rolled_back_live_observation"] is False
    assert details["retained_basis"]["generation"] == 2
    assert details["retained_basis"]["part_id"] == "charge-A2"
    assert c.charge_contactor_observed_closed is False
    assert c.observed_basis("charge")["part_id"] == "charge-A2"
    assert len(c.pending_feedback) == 1
    # Old part history keeps its archived facts; the revoked report is not
    # claimed by either part.
    old_history = next(
        entry for entry in c.part_history
        if entry["part_id"] == "charge-original"
    )
    assert old_history.get("late_feedback", []) == []


def test_revoke_after_reattribution_and_replacement_roundtrip(tmp_path):
    """Full sequence: attribute old feedback, receive new feedback, revoke,
    re-attribute, replace the same numbered part, save and reopen."""
    c = controller()
    trip_overvoltage(c)
    c.register_part_feedback("charge", None, False, 1.5)
    c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-07", "initial guess", 2.0
    )
    c.register_part_feedback("charge", "charge-original", False, 2.5)
    c.revoke_attribution("pf-1", "tech-08", "wrong work order", 3.0)
    c.attribute_pending_feedback(
        "pf-1", "charge-original", 1, "tech-08",
        "confirmed against install sheet", 4.0,
    )
    c.register_contactor_replacement("charge", "charge-A2", 5.0)
    c.register_part_feedback("charge", "charge-A2", False, 5.5)

    snapshot = c.state_snapshot()
    restored = controller()
    restored.restore_state(snapshot)
    assert restored.charge_contactor_observed_closed is False
    basis = restored.observed_basis("charge")
    assert basis["kind"] == "part_feedback"
    assert basis["part_id"] == "charge-A2"
    assert basis["generation"] == 2
    assert restored.part_generations["charge"] == 2
    assert restored.pending_feedback == []
    assert [
        entry["action"] for entry in restored.attribution_log
    ] == ["attributed", "revoked", "attributed"]
    # The original feedback report is preserved unchanged.
    resolved = restored.resolved_feedback
    assert len(resolved) == 1
    original = resolved[0]["feedback"]
    assert original["feedback_id"] == "pf-1"
    assert original["part_id"] is None
    assert original["observed_closed"] is False
    assert original["timestamp"] == 1.5
    assert original["reason"] == "missing_part_id"
    old_history = next(
        entry for entry in restored.part_history
        if entry["part_id"] == "charge-original"
    )
    assert old_history.get("late_feedback", []) == []
