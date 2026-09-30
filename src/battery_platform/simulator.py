from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .models import Measurement


@dataclass
class VirtualContactor:
    """Model a physical contactor: delayed movement, jitter, or a stuck pole.

    ``apply_command`` receives the latest requested closed/open state and a
    virtual timestamp; ``observe`` returns the physical feedback seen at that
    moment. The model never assumes a command has physically taken effect.
    """

    name: str = "contactor"
    initial_closed: bool = True
    actuation_delay: float = 0.0
    stuck: bool = False
    jitter: float = 0.0
    _closed: bool = field(init=False)
    _command: bool = field(init=False)
    _command_since: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._closed = self.initial_closed
        self._command = self.initial_closed

    def apply_command(
        self, requested_closed: bool, timestamp: float
    ) -> None:
        if requested_closed != self._command:
            self._command = requested_closed
            self._command_since = timestamp

    def observe(self, timestamp: float) -> bool:
        if self.stuck:
            return self._closed
        elapsed = (
            timestamp - self._command_since
            if self._command_since is not None
            else float("inf")
        )
        if elapsed < self.actuation_delay:
            return self._closed
        self._closed = self._command
        if self.jitter and self._command_since is not None:
            phase = int(max(0.0, elapsed) / self.jitter)
            if phase % 2 == 1:
                return not self._closed
        return self._closed


@dataclass
class VirtualBatteryPack:
    """Generate raw cell samples; it never emits protection decisions."""

    cell_count: int = 4
    nominal_voltage: float = 3.7
    nominal_temperature: float = 30.0
    time_step: float = 0.2
    time: float = 0.0

    def sample(
        self,
        voltage_fn: Callable[[int, float], float | None] | None = None,
        temperature_fn: Callable[[int, float], float | None] | None = None,
        charge_feedback: bool | None = None,
        discharge_feedback: bool | None = None,
    ) -> Measurement:
        timestamp = self.time
        voltages = [
            self.nominal_voltage
            if voltage_fn is None
            else voltage_fn(index, timestamp)
            for index in range(self.cell_count)
        ]
        temperatures = [
            self.nominal_temperature
            if temperature_fn is None
            else temperature_fn(index, timestamp)
            for index in range(self.cell_count)
        ]
        self.time += self.time_step
        return Measurement(
            timestamp,
            voltages,
            temperatures,
            charge_contactor_feedback=charge_feedback,
            discharge_contactor_feedback=discharge_feedback,
        )

    def boundary_jitter_sequence(self, cycles: int = 4) -> list[Measurement]:
        """Raw samples jittering around voltage and recovery thresholds."""

        def voltage(index: int, timestamp: float) -> float:
            if index != 0:
                return self.nominal_voltage
            if timestamp < 1.0:
                return 4.19
            phase = int((timestamp - 1.0) / self.time_step)
            return 4.201 if phase % 2 == 0 else 4.099

        return [self.sample(voltage_fn=voltage) for _ in range(cycles * 10 + 6)]
