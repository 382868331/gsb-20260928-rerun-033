from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FaultName = Literal[
    "over_voltage",
    "under_voltage",
    "over_temperature",
    "measurement_fault",
]


@dataclass(frozen=True)
class Measurement:
    """Raw pack measurement. None represents a missing sensor value."""

    timestamp: float
    voltages: list[float | None]
    temperatures: list[float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "voltages": list(self.voltages),
            "temperatures": list(self.temperatures),
        }


@dataclass(frozen=True)
class ProtectionEvent:
    timestamp: float
    event_type: str
    charge_contactor_closed: bool
    discharge_contactor_closed: bool
    active_reasons: tuple[str, ...] = field(default_factory=tuple)
    locked_reasons: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "charge_contactor_closed": self.charge_contactor_closed,
            "discharge_contactor_closed": self.discharge_contactor_closed,
            "active_reasons": list(self.active_reasons),
            "locked_reasons": list(self.locked_reasons),
            "reasons": list(self.reasons),
            "details": self.details,
        }
