from __future__ import annotations

from dataclasses import dataclass

from .model import ProtectionKind


@dataclass(frozen=True, slots=True)
class ProtectionThresholds:
    trigger_value: float
    release_value: float
    trigger_seconds: float
    release_seconds: float
    latch: bool = False

    def __post_init__(self) -> None:
        if self.trigger_seconds < 0 or self.release_seconds < 0:
            raise ValueError("protection durations must be non-negative")


@dataclass(frozen=True, slots=True)
class ProtectionConfig:
    over_voltage: ProtectionThresholds
    under_voltage: ProtectionThresholds
    over_temperature: ProtectionThresholds
    measurement_invalid: ProtectionThresholds

    def for_kind(self, kind: ProtectionKind) -> ProtectionThresholds:
        return getattr(self, kind.value)

    def __post_init__(self) -> None:
        if self.over_voltage.release_value >= self.over_voltage.trigger_value:
            raise ValueError("over-voltage release threshold must be below trigger threshold")
        if self.under_voltage.release_value <= self.under_voltage.trigger_value:
            raise ValueError("under-voltage release threshold must be above trigger threshold")
        if self.over_temperature.release_value >= self.over_temperature.trigger_value:
            raise ValueError("over-temperature release threshold must be below trigger threshold")

    @staticmethod
    def default(
        *,
        ov_trigger: float = 4.20,
        ov_release: float = 4.10,
        uv_trigger: float = 2.80,
        uv_release: float = 2.90,
        ot_trigger: float = 55.0,
        ot_release: float = 50.0,
        trigger_seconds: float = 1.0,
        release_seconds: float = 2.0,
        missing_trigger_seconds: float = 0.5,
        missing_release_seconds: float = 0.0,
        latch_over_voltage: bool = False,
        latch_under_voltage: bool = False,
        latch_over_temperature: bool = False,
        latch_measurement_invalid: bool = False,
    ) -> "ProtectionConfig":
        return ProtectionConfig(
            ProtectionThresholds(ov_trigger, ov_release, trigger_seconds, release_seconds, latch_over_voltage),
            ProtectionThresholds(uv_trigger, uv_release, trigger_seconds, release_seconds, latch_under_voltage),
            ProtectionThresholds(ot_trigger, ot_release, trigger_seconds, release_seconds, latch_over_temperature),
            ProtectionThresholds(1.0, 0.0, missing_trigger_seconds, missing_release_seconds, latch_measurement_invalid),
        )
