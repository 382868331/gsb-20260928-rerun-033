from __future__ import annotations

from dataclasses import dataclass

from .config import ProtectionConfig, ProtectionThresholds
from .model import Measurement, ProtectionKind


@dataclass(frozen=True, slots=True)
class Violation:
    kind: ProtectionKind
    detail: str
    value: float | None = None


@dataclass(slots=True)
class _ChannelState:
    active: bool = False
    locked: bool = False
    candidate_since: float | None = None
    healthy_since: float | None = None
    last_timestamp: float | None = None


@dataclass(frozen=True, slots=True)
class ProtectionEvent:
    timestamp: float
    action: str
    kind: ProtectionKind
    reason: str
    recovery_condition: str | None
    detail: str
    latch: bool
    measurement: Measurement


@dataclass(frozen=True, slots=True)
class ProtectionDecision:
    timestamp: float
    contactor_open: bool
    active_protections: tuple[ProtectionKind, ...]
    locked_protections: tuple[ProtectionKind, ...]
    violations: tuple[Violation, ...]
    events: tuple[ProtectionEvent, ...]
    measurement_valid: bool
    measurement: Measurement


class ProtectionController:
    def __init__(self, config: ProtectionConfig) -> None:
        self._config = config
        self._states = {kind: _ChannelState() for kind in ProtectionKind}

    def update(self, measurement: Measurement) -> ProtectionDecision:
        violations = self._inspect(measurement)
        violation_kinds = {violation.kind for violation in violations}
        healthy_kinds = self._healthy_channels(measurement)
        events: list[ProtectionEvent] = []

        for kind in ProtectionKind:
            state = self._states[kind]
            if state.last_timestamp is not None and measurement.timestamp < state.last_timestamp:
                raise ValueError("measurement timestamps must be non-decreasing")
            state.last_timestamp = measurement.timestamp

            thresholds = self._config.for_kind(kind)
            violating = kind in violation_kinds
            healthy = kind in healthy_kinds
            self._update_timer(state, measurement.timestamp, violating, healthy)

            if violating and state.candidate_since is not None:
                duration = measurement.timestamp - state.candidate_since
                if not state.active and duration >= thresholds.trigger_seconds:
                    self._activate(state, kind, thresholds, violations, measurement, events)

            if state.active and healthy and not state.locked:
                healthy_duration = 0.0 if state.healthy_since is None else measurement.timestamp - state.healthy_since
                if healthy_duration >= thresholds.release_seconds:
                    self._release(state, kind, thresholds, measurement, events)

        active = tuple(kind for kind, state in self._states.items() if state.active)
        locked = tuple(kind for kind, state in self._states.items() if state.locked)
        return ProtectionDecision(
            timestamp=measurement.timestamp,
            contactor_open=bool(active),
            active_protections=active,
            locked_protections=locked,
            violations=tuple(violations),
            events=tuple(events),
            measurement_valid=ProtectionKind.MEASUREMENT_INVALID not in violation_kinds,
            measurement=measurement,
        )

    def reset(self, kind: ProtectionKind, measurement: Measurement) -> ProtectionEvent | None:
        state = self._states[kind]
        thresholds = self._config.for_kind(kind)
        if not state.locked:
            return None
        violations = self._inspect(measurement)
        healthy_kinds = self._healthy_channels(measurement)
        state.locked = False
        violating = any(item.kind == kind for item in violations)
        healthy = kind in healthy_kinds
        self._update_timer(state, measurement.timestamp, violating, healthy)
        healthy_duration = 0.0 if state.healthy_since is None else measurement.timestamp - state.healthy_since
        if not violating and healthy_duration >= thresholds.release_seconds:
            state.active = False
            action = "reset_and_recovered"
            recovery = None
        else:
            state.locked = True
            action = "reset_rejected"
            recovery = self._recovery_condition(kind, thresholds)
        return ProtectionEvent(
            timestamp=measurement.timestamp,
            action=action,
            kind=kind,
            reason="manual reset" if action == "reset_and_recovered" else "release condition is not satisfied",
            recovery_condition=recovery,
            detail="manual reset accepted" if action == "reset_and_recovered" else "protection remains locked",
            latch=thresholds.latch,
            measurement=measurement,
        )

    def is_active(self, kind: ProtectionKind) -> bool:
        return self._states[kind].active

    def is_locked(self, kind: ProtectionKind) -> bool:
        return self._states[kind].locked

    def _inspect(self, measurement: Measurement) -> list[Violation]:
        result: list[Violation] = []
        if not measurement.cell_voltages or not measurement.temperatures:
            result.append(Violation(ProtectionKind.MEASUREMENT_INVALID, "measurement frame is empty"))
            return result

        missing: list[str] = []
        for index, voltage in enumerate(measurement.cell_voltages):
            if voltage is None:
                missing.append(f"cell_{index}.voltage")
            else:
                thresholds = self._config.over_voltage
                if voltage >= thresholds.trigger_value:
                    result.append(Violation(ProtectionKind.OVER_VOLTAGE, f"cell {index} voltage {voltage:g} >= {thresholds.trigger_value:g}", voltage))
                thresholds = self._config.under_voltage
                if voltage <= thresholds.trigger_value:
                    result.append(Violation(ProtectionKind.UNDER_VOLTAGE, f"cell {index} voltage {voltage:g} <= {thresholds.trigger_value:g}", voltage))

        for index, temperature in enumerate(measurement.temperatures):
            if temperature is None:
                missing.append(f"sensor_{index}.temperature")
            else:
                thresholds = self._config.over_temperature
                if temperature >= thresholds.trigger_value:
                    result.append(Violation(ProtectionKind.OVER_TEMPERATURE, f"sensor {index} temperature {temperature:g} >= {thresholds.trigger_value:g}", temperature))

        if missing:
            result.append(Violation(ProtectionKind.MEASUREMENT_INVALID, "missing values: " + ", ".join(missing)))
        return result

    def _healthy_channels(self, measurement: Measurement) -> set[ProtectionKind]:
        if not measurement.cell_voltages or not measurement.temperatures:
            return set()
        voltages = [value for value in measurement.cell_voltages if value is not None]
        temperatures = [value for value in measurement.temperatures if value is not None]
        result: set[ProtectionKind] = set()
        if len(voltages) == len(measurement.cell_voltages) and len(temperatures) == len(measurement.temperatures):
            result.add(ProtectionKind.MEASUREMENT_INVALID)
        if voltages:
            if max(voltages) < self._config.over_voltage.release_value:
                result.add(ProtectionKind.OVER_VOLTAGE)
            if min(voltages) > self._config.under_voltage.release_value:
                result.add(ProtectionKind.UNDER_VOLTAGE)
        if temperatures and max(temperatures) < self._config.over_temperature.release_value:
            result.add(ProtectionKind.OVER_TEMPERATURE)
        return result

    @staticmethod
    def _update_timer(state: _ChannelState, timestamp: float, violating: bool, healthy: bool) -> None:
        if violating:
            if state.candidate_since is None:
                state.candidate_since = timestamp
            state.healthy_since = None
        elif healthy:
            state.candidate_since = None
            if state.healthy_since is None:
                state.healthy_since = timestamp
        else:
            state.candidate_since = None
            state.healthy_since = None

    def _activate(self, state: _ChannelState, kind: ProtectionKind, thresholds: ProtectionThresholds,
                  violations: list[Violation], measurement: Measurement, events: list[ProtectionEvent]) -> None:
        violation = next(item for item in violations if item.kind == kind)
        state.active = True
        state.locked = thresholds.latch
        events.append(ProtectionEvent(
            timestamp=measurement.timestamp,
            action="trip",
            kind=kind,
            reason=violation.detail,
            recovery_condition=self._recovery_condition(kind, thresholds),
            detail=f"condition persisted for at least {thresholds.trigger_seconds:g}s",
            latch=thresholds.latch,
            measurement=measurement,
        ))

    def _release(self, state: _ChannelState, kind: ProtectionKind, thresholds: ProtectionThresholds,
                 measurement: Measurement, events: list[ProtectionEvent]) -> None:
        state.active = False
        events.append(ProtectionEvent(
            timestamp=measurement.timestamp,
            action="recover",
            kind=kind,
            reason="release condition remained satisfied",
            recovery_condition=None,
            detail=f"condition stayed in release band for at least {thresholds.release_seconds:g}s",
            latch=thresholds.latch,
            measurement=measurement,
        ))

    @staticmethod
    def _recovery_condition(kind: ProtectionKind, thresholds: ProtectionThresholds) -> str:
        if kind is ProtectionKind.OVER_VOLTAGE:
            return f"all voltages < {thresholds.release_value:g} for {thresholds.release_seconds:g}s"
        if kind is ProtectionKind.UNDER_VOLTAGE:
            return f"all voltages > {thresholds.release_value:g} for {thresholds.release_seconds:g}s"
        if kind is ProtectionKind.OVER_TEMPERATURE:
            return f"all temperatures < {thresholds.release_value:g} for {thresholds.release_seconds:g}s"
        return f"all measurements present for {thresholds.release_seconds:g}s"
