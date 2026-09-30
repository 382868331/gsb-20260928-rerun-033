from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from .models import Measurement, ProtectionEvent


@dataclass(frozen=True)
class FaultRule:
    trigger_threshold: float | None
    recovery_threshold: float | None
    trigger_delay: float
    recovery_delay: float
    locked: bool = True
    direction: str = "above"


@dataclass(frozen=True)
class ProtectionConfig:
    expected_cells: int = 4
    over_voltage: FaultRule = field(
        default_factory=lambda: FaultRule(4.20, 4.10, 1.0, 1.0)
    )
    under_voltage: FaultRule = field(
        default_factory=lambda: FaultRule(3.00, 3.10, 1.0, 1.0, direction="below")
    )
    over_temperature: FaultRule = field(
        default_factory=lambda: FaultRule(55.0, 50.0, 1.0, 1.0)
    )
    measurement_fault: FaultRule = field(
        default_factory=lambda: FaultRule(None, None, 0.0, 0.5)
    )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProtectionConfig":
        rule_names = (
            "over_voltage",
            "under_voltage",
            "over_temperature",
            "measurement_fault",
        )
        kwargs: dict[str, object] = {}
        if "expected_cells" in payload:
            kwargs["expected_cells"] = int(payload["expected_cells"])  # type: ignore[arg-type]
        for name in rule_names:
            value = payload.get(name)
            if value is not None:
                kwargs[name] = FaultRule(**value)  # type: ignore[arg-type]
        return cls(**kwargs)


@dataclass
class _FaultState:
    candidate_since: float | None = None
    active: bool = False
    recovery_candidate_since: float | None = None
    recovery_ready: bool = False
    locked: bool = False
    last_value: float | None = None
    source: str = ""


# Observation relative to the trigger/recovery thresholds.
# "fault" means beyond the trigger threshold, "safe" means beyond the
# recovery threshold, "hysteresis" means between the two thresholds, and
# "inactive" is used by threshold-less rules (e.g. measurement fault).
_FAULT = "fault"
_SAFE = "safe"
_HYSTERESIS = "hysteresis"
_INACTIVE = "inactive"


class ProtectionController:
    """Debounced, latching protection decision engine."""

    _ACTIONS: ClassVar[dict[str, str]] = {
        "over_voltage": "open_charge_contactor",
        "under_voltage": "open_discharge_contactor",
        "over_temperature": "open_both_contactors",
        "measurement_fault": "open_both_contactors",
    }
    _CONTACTOR_EFFECT: ClassVar[dict[str, tuple[bool, bool]]] = {
        "open_charge_contactor": (True, False),
        "open_discharge_contactor": (False, True),
        "open_both_contactors": (True, True),
    }

    def __init__(self, config: ProtectionConfig | None = None):
        self.config = config or ProtectionConfig()
        self._states = {
            name: _FaultState()
            for name in (
                "over_voltage",
                "under_voltage",
                "over_temperature",
                "measurement_fault",
            )
        }
        self.events: list[ProtectionEvent] = []
        self.current_timestamp: float | None = None

    @property
    def charge_contactor_closed(self) -> bool:
        return not self._contactors_blocked()[0]

    @property
    def discharge_contactor_closed(self) -> bool:
        return not self._contactors_blocked()[1]

    @property
    def active_reasons(self) -> tuple[str, ...]:
        return tuple(
            name for name, state in self._states.items() if state.active or state.locked
        )

    def update(self, measurement: Measurement) -> list[ProtectionEvent]:
        before_charge = self.charge_contactor_closed if self.events else True
        before_discharge = self.discharge_contactor_closed if self.events else True
        self.current_timestamp = measurement.timestamp

        observations = self._observe(measurement)
        produced: list[ProtectionEvent] = []
        for name, condition in observations.items():
            event = self._update_fault(name, condition, measurement.timestamp)
            if event is not None:
                produced.append(event)

        after_charge = self.charge_contactor_closed
        after_discharge = self.discharge_contactor_closed
        if (
            not self.events
            or before_charge != after_charge
            or before_discharge != after_discharge
        ):
            produced.append(
                self._make_event(
                    "contactor_state" if self.events else "initial_state",
                    reasons=(),
                    details=self._recovery_summary(),
                )
            )

        self.events.extend(produced)
        return produced

    def request_reset(self, timestamp: float | None = None) -> list[ProtectionEvent]:
        reset_timestamp = self.current_timestamp if timestamp is None else timestamp
        if reset_timestamp is None:
            raise ValueError("timestamp is required before the first reset")

        reset_names = []
        blocked = []
        for name, state in self._states.items():
            if not state.locked:
                continue
            if state.active or not state.recovery_ready:
                blocked.append(name)
                continue
            state.locked = False
            state.recovery_ready = False
            state.recovery_candidate_since = None
            reset_names.append(name)

        details: dict[str, object] = {"reset_reasons": reset_names}
        if blocked and not reset_names:
            details["blocked_reasons"] = blocked
            details["recovery_conditions"] = self._recovery_summary()
            event_type = "reset_blocked"
        else:
            if blocked:
                details["blocked_reasons"] = blocked
                details["recovery_conditions"] = self._recovery_summary()
            event_type = "manual_reset" if reset_names else "reset_idle"
        event = self._make_event(event_type, reasons=tuple(reset_names), details=details)
        self.events.append(event)
        return [event]

    def replay(self) -> list[ProtectionEvent]:
        return list(self.events)

    def _observe(self, measurement: Measurement) -> dict[str, str]:
        conditions = {name: _INACTIVE for name in self._states}
        missing_sources: list[str] = []

        voltages = list(measurement.voltages)
        temperatures = list(measurement.temperatures)
        if len(voltages) != self.config.expected_cells:
            missing_sources.append(f"voltage_count={len(voltages)}")
            voltages = voltages[: self.config.expected_cells]
            voltages += [None] * (self.config.expected_cells - len(voltages))
        if len(temperatures) != self.config.expected_cells:
            missing_sources.append(f"temperature_count={len(temperatures)}")
            temperatures = temperatures[: self.config.expected_cells]
            temperatures += [None] * (self.config.expected_cells - len(temperatures))

        for index, value in enumerate(voltages):
            source = f"cell_{index + 1}_voltage"
            if value is None:
                missing_sources.append(source)
                continue
            value_f = float(value)
            condition = self._classify(self.config.over_voltage, value_f)
            if self._worse(condition, conditions["over_voltage"]):
                conditions["over_voltage"] = condition
            if condition == _FAULT:
                self._remember("over_voltage", value_f, source)
            under_condition = self._classify(
                self.config.under_voltage, value_f
            )
            if self._worse(under_condition, conditions["under_voltage"]):
                conditions["under_voltage"] = under_condition
            if under_condition == _FAULT:
                self._remember("under_voltage", value_f, source)

        for index, value in enumerate(temperatures):
            source = f"cell_{index + 1}_temperature"
            if value is None:
                missing_sources.append(source)
                continue
            value_f = float(value)
            condition = self._classify(self.config.over_temperature, value_f)
            if self._worse(condition, conditions["over_temperature"]):
                conditions["over_temperature"] = condition
            if condition == _FAULT:
                self._remember("over_temperature", value_f, source)

        if missing_sources:
            conditions["measurement_fault"] = _FAULT
            state = self._states["measurement_fault"]
            state.last_value = None
            state.source = ",".join(missing_sources)
        else:
            conditions["measurement_fault"] = _SAFE

        return conditions

    def _update_fault(
        self, name: str, condition: str, timestamp: float
    ) -> ProtectionEvent | None:
        rule = getattr(self.config, name)
        state = self._states[name]

        if condition == _FAULT:
            if state.candidate_since is None:
                state.candidate_since = timestamp
            state.recovery_candidate_since = None
            state.recovery_ready = False
            if (
                not state.active
                and timestamp - state.candidate_since + 1e-9 >= rule.trigger_delay
            ):
                state.active = True
                state.recovery_ready = False
                if rule.locked:
                    state.locked = True
                return self._fault_event("protection_triggered", name)
            return None

        # Outside the trigger zone the trigger debounce must restart.
        state.candidate_since = None

        # The hysteresis band keeps the fault active without counting toward
        # recovery; leaving the safe side restarts the recovery timer.
        if condition != _SAFE:
            interrupted = state.recovery_candidate_since is not None
            state.recovery_candidate_since = None
            # Leaving the safe side cancels both in-progress recovery and
            # previously reached readiness: the value is no longer safe.
            state.recovery_ready = False
            if interrupted and state.active:
                return self._fault_event("recovery_interrupted", name)
            return None

        if not state.active and not state.recovery_ready:
            state.recovery_candidate_since = None
            state.recovery_ready = False
            return None

        if state.recovery_candidate_since is None:
            state.recovery_candidate_since = timestamp
        if (
            timestamp - state.recovery_candidate_since + 1e-9
            < rule.recovery_delay
        ):
            return None

        state.active = False
        state.recovery_candidate_since = None
        if state.locked:
            state.recovery_ready = True
            return self._fault_event(
                "recovery_ready_manual_reset_required",
                name,
                extra_details={"action_remains": self._ACTIONS[name]},
            )

        state.recovery_ready = False
        return self._fault_event("protection_released", name)

    @staticmethod
    def _worse(candidate: str, current: str) -> bool:
        rank = {_INACTIVE: 0, _SAFE: 1, _HYSTERESIS: 2, _FAULT: 3}
        return rank[candidate] > rank[current]

    def _classify(self, rule: FaultRule, value: float) -> str:
        threshold = rule.trigger_threshold
        if threshold is None:
            return _INACTIVE
        recovery = rule.recovery_threshold
        if rule.direction == "above":
            if value >= threshold:
                return _FAULT
            if recovery is None or value <= recovery:
                return _SAFE
            return _HYSTERESIS
        if value <= threshold:
            return _FAULT
        if recovery is None or value >= recovery:
            return _SAFE
        return _HYSTERESIS

    def _remember(self, name: str, value: float, source: str) -> None:
        state = self._states[name]
        state.last_value = value
        state.source = source

    def _contactors_blocked(self) -> tuple[bool, bool]:
        charge = False
        discharge = False
        for name, state in self._states.items():
            if not state.active and not state.locked:
                continue
            action = self._ACTIONS[name]
            blocks_charge, blocks_discharge = self._CONTACTOR_EFFECT[action]
            charge = charge or blocks_charge
            discharge = discharge or blocks_discharge
        return charge, discharge

    def _fault_event(
        self,
        event_type: str,
        name: str,
        extra_details: dict[str, object] | None = None,
    ) -> ProtectionEvent:
        state = self._states[name]
        rule = getattr(self.config, name)
        details = {
            "action": self._ACTIONS[name],
            "source": state.source,
            "last_value": state.last_value,
            "trigger_threshold": rule.trigger_threshold,
            "recovery_threshold": rule.recovery_threshold,
            "trigger_delay": rule.trigger_delay,
            "recovery_delay": rule.recovery_delay,
            "locked": rule.locked,
        }
        if extra_details:
            details.update(extra_details)
        return self._make_event(event_type, reasons=(name,), details=details)

    def _make_event(
        self,
        event_type: str,
        reasons: tuple[str, ...],
        details: dict[str, object],
    ) -> ProtectionEvent:
        return ProtectionEvent(
            timestamp=float(self.current_timestamp or 0.0),
            event_type=event_type,
            charge_contactor_closed=self.charge_contactor_closed,
            discharge_contactor_closed=self.discharge_contactor_closed,
            active_reasons=self.active_reasons,
            locked_reasons=tuple(
                name for name, state in self._states.items() if state.locked
            ),
            reasons=reasons,
            details=details,
        )

    def _recovery_summary(self) -> dict[str, object]:
        summary = {}
        for name, state in self._states.items():
            if not state.active and not state.locked:
                continue
            rule = getattr(self.config, name)
            summary[name] = {
                "active": state.active,
                "locked": state.locked,
                "recovery_ready": state.recovery_ready,
                "recovery_threshold": rule.recovery_threshold,
                "recovery_delay": rule.recovery_delay,
                "manual_reset_required": rule.locked,
            }
        return summary
