from battery_protection.config import ProtectionConfig
from battery_protection.controller import ProtectionController
from battery_protection.model import Measurement, ProtectionKind


def sample(timestamp, voltage, temperature=25.0):
    return Measurement(timestamp, (voltage, 3.7), (temperature,))


def test_release_hysteresis_band_keeps_protection_active():
    controller = ProtectionController(ProtectionConfig.default(
        trigger_seconds=0.0,
        release_seconds=1.0,
    ))

    assert controller.update(sample(0.0, 4.25)).events[0].action == "trip"
    band = controller.update(sample(1.0, 4.15))
    assert band.events == ()
    assert controller.is_active(ProtectionKind.OVER_VOLTAGE)

    still_band = controller.update(sample(3.0, 4.15))
    assert still_band.events == ()
    assert controller.is_active(ProtectionKind.OVER_VOLTAGE)


def test_value_inside_hysteresis_band_does_not_trrip_from_normal():
    controller = ProtectionController(ProtectionConfig.default(trigger_seconds=0.0))
    decision = controller.update(sample(0.0, 4.15))
    assert decision.events == ()
    assert not decision.contactor_open
