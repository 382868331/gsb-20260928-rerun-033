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
    value=3.7,
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
    c = ProtectionController(config())
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    return c


def test_pending_view_shows_feedback_reason_and_installations():
    c = controller()
    c.register_part_feedback("charge", None, False, 1.0)

    view = c.pending_feedback_view()
    assert len(view) == 1
    entry = view[0]
    assert entry["feedback_id"] == 1
    assert entry["reason"] == "missing_part_id"
    assert entry["observed_closed"] is False
    assert entry["status"] == "pending"
    assert entry["installation_at_feedback"]["part_id"] == "charge-original"
    assert entry["installation_at_feedback"]["generation"] == 1
    assert entry["ambiguity"] == []


def test_attribution_records_decision_without_rewriting_feedback():
    c = controller()
    c.register_part_feedback("charge", None, False, 1.0)

    produced = c.attribute_pending_feedback(
        1, "charge-original", 1, "tech-1", "serial confirmed", 2.0
    )
    assert produced[0].event_type == "part_feedback_attributed"
    assert produced[0].details["operator"] == "tech-1"
    assert produced[0].details["applied_to_live_state"] is False

    history = c.attribution_history
    assert len(history) == 1
    decision = history[0]
    assert decision["status"] == "active"
    assert decision["operator"] == "tech-1"
    assert decision["reason"] == "serial confirmed"
    assert decision["timestamp"] == 2.0

    pending = c.pending_feedback
    assert pending[0]["status"] == "attributed"
    assert pending[0]["decision_id"] == decision["decision_id"]
    assert pending[0]["part_id"] is None
    assert pending[0]["reason"] == "missing_part_id"
    assert c.pending_feedback_view() == []
    # The attributed report is not applied to the live contactor state.
    assert c.charge_contactor_observed_closed is True


def test_duplicate_attribution_is_idempotent():
    c = controller()
    c.register_part_feedback("charge", None, False, 1.0)
    first = c.attribute_pending_feedback(
        1, "charge-original", 1, "tech-1", "serial confirmed", 2.0
    )
    again = c.attribute_pending_feedback(
        1, "charge-original", 1, "tech-1", "serial confirmed", 3.0
    )
    assert again == []
    assert len(c.attribution_history) == 1
    assert len([e for e in c.replay()
                if e.event_type == "part_feedback_attributed"]) == +        len(first)


def test_conflicting_attribution_requires_revocation_first():
    c = controller()
    c.register_contactor_replacement("charge", "charge-A2", 1.0)
    c.register_part_feedback("charge", None, False, 2.0)
    c.attribute_pending_feedback(1, "charge-A2", 2, "tech-1", "guess", 3.0)
    with pytest.raises(ValueError, match="revoke"):
        c.attribute_pending_feedback(
            1, "charge-original", 1, "tech-2", "correction", 4.0
        )


def test_revoke_and_reattribute_keeps_full_history():
    c = controller()
    c.register_contactor_replacement("charge", "charge-A2", 1.0)
    c.register_part_feedback("charge", None, False, 2.0)
    decision = c.attribute_pending_feedback(
        1, "charge-A2", 2, "tech-1", "initial guess", 3.0
    )[0]

    revoked = c.revoke_attribution(
        decision.details["decision_id"], "tech-2", "wrong serial", 4.0
    )
    assert revoked.event_type == "part_feedback_attribution_revoked"
    assert revoked.details["operator"] == "tech-2"

    history = c.attribution_history
    assert history[0]["status"] == "revoked"
    assert history[0]["revoked_by"] == "tech-2"
    assert history[0]["revoked_at"] == 4.0
    assert history[0]["revocation_reason"] == "wrong serial"
    assert c.pending_feedback[0]["status"] == "pending"
    assert len(c.pending_feedback_view()) == 1

    c.attribute_pending_feedback(
        1, "charge-original", 1, "tech-2", "corrected", 5.0
    )
    history = c.attribution_history
    assert [item["status"] for item in history] == ["revoked", "active"]
    assert history[1]["part_id"] == "charge-original"
    assert history[1]["generation"] == 1
    assert c.pending_feedback[0]["status"] == "attributed"

    with pytest.raises(ValueError, match="already revoked"):
        c.revoke_attribution(
            decision.details["decision_id"], "tech-3", "again", 6.0
        )


def test_attribution_requires_a_real_installation():
    c = controller()
    c.register_contactor_replacement("charge", "charge-A2", 1.0)
    c.register_part_feedback("charge", None, False, 2.0)
    with pytest.raises(ValueError, match="no installation"):
        c.attribute_pending_feedback(
            1, "charge-original", 2, "tech-1", "wrong generation", 3.0
        )
    with pytest.raises(ValueError, match="unknown pending feedback"):
        c.attribute_pending_feedback(
            99, "charge-A2", 2, "tech-1", "missing", 3.0
        )


def test_attributed_history_report_cannot_verify_current_recovery():
    c = ProtectionController(config())
    c.update(sample(0.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0))
    # Over-voltage opens the charge contactor; it stays welded shut.
    c.update(sample(1.0, charge_feedback=True, discharge_feedback=True,
                    load_voltage=48.0, value=4.21))
    c.update(sample(2.0, charge_feedback=True, discharge_feedback=False,
                    load_voltage=48.0, value=4.1))
    assert "charge_contactor_mismatch" in c.active_reasons

    # An unattributed "open" report arrives and is manually attributed to
    # the currently installed part: it still must not touch the live
    # feedback state nor unblock the reset.
    c.register_part_feedback("charge", None, False, 3.0)
    c.attribute_pending_feedback(
        1, "charge-original", 1, "tech-1", "paper trail", 4.0
    )
    assert c.charge_contactor_observed_closed is True
    assert "charge_contactor_mismatch" in c.active_reasons
    assert "charge_contactor_mismatch" in c.reset_blocked_reasons


def test_ambiguity_flags_for_reinstalls_late_reports_and_retractions():
    c = controller()
    c.register_contactor_replacement("charge", "charge-A2", 1.0)
    c.retract_contactor_replacement("charge", 2.0, "mislogged swap")
    # Late report from the retired part after the retraction.
    c.register_part_feedback("charge", "charge-A2", True, 3.0)
    c.register_part_feedback("charge", None, False, 4.0)

    view = c.pending_feedback_view()
    assert len(view) == 1
    ambiguity = view[0]["ambiguity"]
    assert "multiple_installations" in ambiguity
    assert "repeated_part_installations" in ambiguity
    assert "late_feedback_recorded" in ambiguity
    assert "replacement_retracted" in ambiguity
    generations = {
        (record["part_id"], record["generation"])
        for record in view[0]["installation_records"]
    }
    assert ("charge-original", 1) in generations
    assert ("charge-A2", 2) in generations
    assert ("charge-original", 3) in generations


def test_attribution_archives_into_matching_part_history():
    c = controller()
    c.register_contactor_replacement("charge", "charge-A2", 1.0)
    c.register_part_feedback("charge", None, False, 2.0)
    c.attribute_pending_feedback(
        1, "charge-original", 1, "tech-1", "dated before swap", 3.0
    )
    archived = c.part_history[0]["attributed_feedback"]
    assert archived[0]["observed_closed"] is False
    assert archived[0]["operator"] == "tech-1"
    assert archived[0]["timestamp"] == 2.0


def test_attribution_survives_snapshot_restore_and_replay(tmp_path):
    path = tmp_path / "log.jsonl"
    recorder = JsonlEventRecorder(path)
    c = ProtectionController(config())
    initial = sample(0.0, charge_feedback=True, discharge_feedback=True,
                     load_voltage=48.0)
    recorder.record_measurement(initial)
    recorder.record_events(c.update(initial))
    recorder.record_controller_state(c)
    recorder.record_part_feedback(c, "charge", None, False, 1.0)
    recorder.record_attribution(
        c, 1, "charge-original", 1, "tech-1", "serial confirmed", 2.0
    )
    recorder.record_attribution_revocation(
        c, 1, "tech-2", "wrong serial", 3.0
    )
    recorder.record_attribution(
        c, 1, "charge-original", 1, "tech-2", "corrected", 4.0
    )

    rebooted = restore_controller(path)
    assert rebooted.attribution_history == c.attribution_history
    assert rebooted.pending_feedback == c.pending_feedback
    assert rebooted.pending_feedback_view() == c.pending_feedback_view()
    assert rebooted.active_reasons == c.active_reasons

    # New feedback after the restart keeps unique ids.
    events = rebooted.register_part_feedback("discharge", None, True, 5.0)
    assert events[0].details["feedback_id"] == 2

    trace = replay_recording(path)
    assert trace[-1]["pending_feedback"] == c.pending_feedback
