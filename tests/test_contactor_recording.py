from battery_platform.models import ContactorFeedback, Measurement
from battery_platform.protection import (
    ContactorFeedbackConfig,
    ProtectionConfig,
    ProtectionController,
)
from battery_platform.recorder import (
    JsonlEventRecorder,
    replay_recording,
    restore_controller,
)


def config():
    return ProtectionConfig(
        contactor_feedback=ContactorFeedbackConfig(
            mismatch_delay=1.0,
            recovery_delay=1.0,
        )
    )


def test_feedback_trace_records_and_replays(tmp_path):
    path = tmp_path / "contactor.jsonl"
    recorder = JsonlEventRecorder(path)
    controller = ProtectionController(config())

    samples = [
        Measurement(0.0, [3.7] * 4, [30.0] * 4),
        Measurement(1.0, [4.25] * 4, [30.0] * 4),
        Measurement(2.0, [4.25] * 4, [30.0] * 4),
    ]
    feedbacks = [
        ContactorFeedback(0.0, False, False),
        ContactorFeedback(2.0, True, True),
        ContactorFeedback(3.0, True, True),
    ]

    recorder.record_measurement(samples[0])
    recorder.record_events(controller.update(samples[0]))
    recorder.record_feedback(controller, feedbacks[0])
    recorder.record_controller_state(controller)
    recorder.record_measurement(samples[1])
    recorder.record_events(controller.update(samples[1]))
    recorder.record_measurement(samples[2])
    recorder.record_events(controller.update(samples[2]))
    recorder.record_feedback(controller, feedbacks[1])
    recorder.record_feedback(controller, feedbacks[2])
    recorder.record_controller_state(controller)

    restored = restore_controller(path, config())
    assert restored.observed_charge_contactor_closed() is True
    assert restored.charge_contactor_closed is False
    assert "charge_contactor_feedback" in restored.active_reasons

    trace = replay_recording(path, config())
    assert trace[-1]["charge_contactor_closed"] is False
    assert trace[-1]["discharge_contactor_closed"] is True


def test_repower_decision_replays(tmp_path):
    path = tmp_path / "repower.jsonl"
    recorder = JsonlEventRecorder(path)
    controller = ProtectionController(config())

    controller.update(Measurement(0.0, [3.7] * 4, [30.0] * 4))
    recorder.record_feedback(
        controller, ContactorFeedback(0.0, False, False)
    )
    recorder.record_repower(controller, 0.1)

    trace = replay_recording(path, config())
    assert trace[-1]["charge_contactor_closed"] is True
