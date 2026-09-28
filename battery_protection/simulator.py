from __future__ import annotations

from dataclasses import dataclass

from .model import Measurement


@dataclass
class VirtualBatteryPack:
    """Produce raw measurements only; protection conclusions stay in the controller."""

    cell_voltages: list[float | None]
    temperatures: list[float | None]
    source: str = "virtual_pack"

    def __init__(self, cell_count: int = 4, temperature_sensor_count: int = 1) -> None:
        if cell_count <= 0 or temperature_sensor_count <= 0:
            raise ValueError("the pack must contain at least one cell and one temperature sensor")
        self.cell_voltages = [3.7] * cell_count
        self.temperatures = [25.0] * temperature_sensor_count
        self.source = "virtual_pack"

    def set_voltage(self, cell: int, voltage: float | None) -> None:
        self._check_index(cell, len(self.cell_voltages), "cell")
        self.cell_voltages[cell] = voltage

    def set_temperature(self, sensor: int, temperature: float | None) -> None:
        self._check_index(sensor, len(self.temperatures), "temperature sensor")
        self.temperatures[sensor] = temperature

    def measure(self, timestamp: float) -> Measurement:
        return Measurement(
            timestamp=float(timestamp),
            cell_voltages=tuple(self.cell_voltages),
            temperatures=tuple(self.temperatures),
            source=self.source,
        )

    @staticmethod
    def _check_index(index: int, length: int, label: str) -> None:
        if not 0 <= index < length:
            raise IndexError(f"{label} index {index} is outside 0..{length - 1}")
