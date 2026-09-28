from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import Measurement


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
        return Measurement(timestamp, voltages, temperatures)

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
