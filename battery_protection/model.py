from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProtectionKind(str, Enum):
    OVER_VOLTAGE = "over_voltage"
    UNDER_VOLTAGE = "under_voltage"
    OVER_TEMPERATURE = "over_temperature"
    MEASUREMENT_INVALID = "measurement_invalid"


@dataclass(frozen=True, slots=True)
class Measurement:
    """Raw observation. ``None`` means the measurement is missing."""

    timestamp: float
    cell_voltages: tuple[float | None, ...]
    temperatures: tuple[float | None, ...]
    source: str = "virtual_pack"
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def has_missing_value(self) -> bool:
        return any(value is None for value in (*self.cell_voltages, *self.temperatures))
