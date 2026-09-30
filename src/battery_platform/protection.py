from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from .models import ContactorFeedback, Measurement, ProtectionEvent


@dataclass(frozen=True)
class FaultRule:
    trigger_threshold: float | None
    recovery_threshold: float | None
    trigger_delay: float
    recovery_delay: float
    locked: bool = True
    direction: str = "above"


@dataclass(frozen=True)
class ContactorFeedbackConfig:
    """Timing for commanded-vs-observed contactor diagnostics."""

    mismatch_delay: float = 1.0
    recovery_delay: float = 1.0
    locked: bool = True


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
    contactor_feedback: ContactorFeedbackConfig = field(
        default_factory=ContactorFeedbackConfig
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
        feedback = payload.get("contactor_feedback")
        if feedback is not None:
            kwargs["contactor_feedback"] = ContactorFeedbackConfig(**feedback)  # type: ignore[arg-type]
        return cls(**kwargs)


@dataclass
class _FaultState:
    observed: bool = False
    candidate_since: float | None = None
    active: bool = False
    recovery_candidate_since: float | None = None
    recovery_ready: bool = False
    locked: bool = False
    last_value: float | None = None
    source: str = ""


@dataclass
class _ContactorState:
    """Separately tracks the commanded and the physically observed state."""

    commanded_closed: bool = False
    observed_closed: bool | None = None
    mismatch_since: float | None = None
    active: bool = False
    locked: bool = False
    recovery_candidate_since: float | None = None
    recovery_ready: bool = False
    boot_verified: bool = False

    @property
    def mismatched(self) -> bool:
        return (
            self.observed_closed is not None
            and self.observed_closed != self.commanded_closed
        )


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
    _CONTACTOR_SIDES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("charge", "charge_contactor_feedback"),
        ("discharge", "discharge_contactor_feedback"),
    )

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
        self._contactor_states = {
            side: _ContactorState() for side, _ in self._CONTACTOR_SIDES
        }
        self._feedback_seen = False
        self.events: list[ProtectionEvent] = []
        self.current_timestamp: float | None = None

    @property
    def charge_contactor_closed(self) -> bool:
        return self._commanded_closed()[0]

    @property
    def discharge_contactor_closed(self) -> bool:
        return self._commanded_closed()[1]

    def observed_charge_contactor_closed(self) -> bool | None:
        return self._contactor_states["charge"].observed_closed

    def observed_discharge_contactor_closed(self) -> bool | None:
        return self._contactor_states["discharge"].observed_closed

    @property
    def power_on_ready(self) -> bool:
        return all(
            not state.locked and not state.active
            for state in self._contactor_states.values()
        )

    @property
    def active_reasons(self) -> tuple[str, ...]:
        reasons = [
            name for name, state in self._states.items() if state.active or state.locked
        ]
        for side, name in self._CONTACTOR_SIDES:
            state = self._contactor_states[side]
            if state.active or state.locked:
                reasons.append(name)
        return tuple(reasons)

    @property
    def reset_ready_reasons(self) -> tuple[str, ...]:
        return tuple(
            name for name, state in self._states.items() if self._reset_ready(name)
        )

    @property
    def reset_blocked_reasons(self) -> tuple[str, ...]:
        reasons = [
            name
            for name, state in self._states.items()
            if state.locked and not self._reset_ready(name)
        ]
        for side, name in self._CONTACTOR_SIDES:
            state = self._contactor_states[side]
            if state.locked and not self._contactor_reset_ready(state):
                reasons.append(name)
        return tuple(reasons)

    def update(
        self,
        measurement: Measurement,
        feedback: ContactorFeedback | None = None,
    ) -> list[ProtectionEvent]:
        before_charge = self.charge_contactor_closed if self.events else True
        before_discharge = self.discharge_contactor_closed if self.events else True
        self.current_timestamp = measurement.timestamp

        observations = self._observe(measurement)
        produced: list[ProtectionEvent] = []
        for name, observed in observations.items():
            event = self._update_fault(name, observed, measurement.timestamp)
            if event is not None:
                produced.append(event)

        if feedback is not None:
            produced.extend(self.update_feedback(feedback, emit_state=False))

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

    def update_feedback(
        self,
        feedback: ContactorFeedback,
        emit_state: bool = True,
    ) -> list[ProtectionEvent]:
        """Advance contactor diagnostics with one physical feedback sample."""

        timestamp = feedback.timestamp
        self.current_timestamp = timestamp
        self._feedback_seen = True

        observed = {
            "charge": feedback.charge_closed,
            "discharge": feedback.discharge_closed,
        }
        produced: list[ProtectionEvent] = []
        for side, _ in self._CONTACTOR_SIDES:
            self._contactor_states[side].observed_closed = observed[side]
        commanded = self._commanded_closed()
        for side, name in self._CONTACTOR_SIDES:
            event = self._update_contactor(side, name, observed[side], timestamp)
            if event is not None:
                produced.append(event)

        new_commanded = self._commanded_closed()
        changed = new_commanded != commanded
        state_event_type = (
            "contactor_state" if (self.events or produced) else "initial_state"
        )
        if emit_state or state_event_type == "initial_state" or changed:
            produced.append(
                self._make_event(
                    state_event_type,
                    reasons=(),
                    details=self._recovery_summary(),
                )
            )
        self.events.extend(produced)
        return produced

    def repower(self, timestamp: float) -> list[ProtectionEvent]:
        """Simulate a power cycle; re-runs the pre-energize contactor check."""

        self.current_timestamp = timestamp
        for state in self._contactor_states.values():
            state.boot_verified = False

        blocked = [
            name
            for side, name in self._CONTACTOR_SIDES
            if self._contactor_states[side].active
            or self._contactor_states[side].locked
            or self._contactor_states[side].observed_closed is not False
        ]

        if blocked:
            event = self._make_event(
                "repower_blocked",
                reasons=tuple(blocked),
                details={"active_reasons": self.active_reasons},
            )
            self.events.append(event)
            return [event]

        for state in self._contactor_states.values():
            state.boot_verified = True
        event = self._make_event(
            "power_on_ready",
            reasons=(),
            details={"preconditions": "contactors_commanded_open_and_observed_open"},
        )
        self.events.append(event)
        return [event]

    def request_reset(self, timestamp: float | None = None) -> list[ProtectionEvent]:
        reset_timestamp = self.current_timestamp if timestamp is None else timestamp
        if reset_timestamp is None:
            raise ValueError("timestamp is required before the first reset")

        reset_names = []
        blocked = []
        for name, state in self._states.items():
            if not state.locked:
                continue
            if state.active or not self._reset_ready(name, reset_timestamp):
                blocked.append(name)
                continue
            state.locked = False
            state.recovery_ready = False
            state.recovery_candidate_since = None
            state.candidate_since = None
            reset_names.append(name)

        for side, name in self._CONTACTOR_SIDES:
            state = self._contactor_states[side]
            if not state.locked:
                continue
            if state.active or not self._contactor_reset_ready(state):
                blocked.append(name)
                continue
            state.locked = False
            state.recovery_ready = False
            state.recovery_candidate_since = None
            state.mismatch_since = None
            reset_names.append(name)

        details: dict[str, object] = {"reset_reasons": reset_names}
        if blocked:
            details["blocked_reasons"] = blocked
            details["recovery_conditions"] = self._recovery_summary()
            event_type = "manual_reset" if reset_names else "reset_blocked"
        else:
            event_type = "manual_reset" if reset_names else "reset_idle"
        event = self._make_event(event_type, reasons=tuple(reset_names), details=details)
        self.events.append(event)
        return [event]

    def replay(self) -> list[ProtectionEvent]:
        return list(self.events)

    def state_snapshot(self) -> dict[str, object]:
        return {
            "timestamp": self.current_timestamp,
            "states": {
                name: {
                    "observed": state.observed,
                    "candidate_since": state.candidate_since,
                    "active": state.active,
                    "recovery_candidate_since": state.recovery_candidate_since,
                    "recovery_ready": state.recovery_ready,
                    "locked": state.locked,
                    "last_value": state.last_value,
                    "source": state.source,
                }
                for name, state in self._states.items()
            },
            "contactor_states": {
                side: {
                    "commanded_closed": state.commanded_closed,
                    "observed_closed": state.observed_closed,
                    "mismatch_since": state.mismatch_since,
                    "active": state.active,
                    "locked": state.locked,
                    "recovery_candidate_since": state.recovery_candidate_since,
                    "recovery_ready": state.recovery_ready,
                    "boot_verified": state.boot_verified,
                }
                for side, state in self._contactor_states.items()
            },
            "feedback_seen": self._feedback_seen,
        }

    def restore_state(self, snapshot: dict[str, object]) -> None:
        self.current_timestamp = snapshot["timestamp"]  # type: ignore[assignment]
        states = snapshot["states"]
        assert isinstance(states, dict)
        for name, payload in states.items():
            assert isinstance(payload, dict)
            state = self._states[name]
            for field_name in (
                "observed",
                "candidate_since",
                "active",
                "recovery_candidate_since",
                "recovery_ready",
                "locked",
                "last_value",
                "source",
            ):
                setattr(state, field_name, payload[field_name])
        contactors = snapshot.get("contactor_states")
        if isinstance(contactors, dict):
            for side, payload in contactors.items():
                assert isinstance(payload, dict)
                state = self._contactor_states[side]
                for field_name in (
                    "commanded_closed",
                    "observed_closed",
                    "mismatch_since",
                    "active",
                    "locked",
                    "recovery_candidate_since",
                    "recovery_ready",
                    "boot_verified",
                ):
                    setattr(state, field_name, payload[field_name])
        self._feedback_seen = bool(snapshot.get("feedback_seen", contactors is not None))

    def _commanded_closed(self) -> tuple[bool, bool]:
        blocked_charge, blocked_discharge = self._contactors_blocked()
        charge = not blocked_charge
        discharge = not blocked_discharge
        pending_precheck = self._feedback_seen and not all(
            state.boot_verified for state in self._contactor_states.values()
        )
        for side, closed in (("charge", charge), ("discharge", discharge)):
            state = self._contactor_states[side]
            if pending_precheck and closed and not state.boot_verified:
                closed = False
            if state.commanded_closed != closed:
                state.mismatch_since = None
                state.recovery_candidate_since = None
                state.recovery_ready = False
            state.commanded_closed = closed
        return (
            self._contactor_states["charge"].commanded_closed,
            self._contactor_states["discharge"].commanded_closed,
        )

    def _update_contactor(
        self,
        side: str,
        name: str,
        observed_closed: bool | None,
        timestamp: float,
    ) -> ProtectionEvent | None:
        config = self.config.contactor_feedback
        state = self._contactor_states[side]
        state.observed_closed = observed_closed

        if observed_closed is None:
            if state.mismatch_since is None:
                state.mismatch_since = timestamp
            if state.active or (state.locked and not state.recovery_ready):
                self._restart_contactor_recovery(state)
            if (
                not state.active
                and timestamp - state.mismatch_since + 1e-9 >= config.mismatch_delay
            ):
                state.active = True
                if config.locked:
                    state.locked = True
                return self._contactor_event(
                    "contactor_feedback_fault",
                    side,
                    name,
                )
            return None

        mismatched = observed_closed != state.commanded_closed
        if mismatched:
            if state.mismatch_since is None:
                state.mismatch_since = timestamp
            if state.active or (state.locked and not state.recovery_ready):
                self._restart_contactor_recovery(state)
            if (
                not state.active
                and timestamp - state.mismatch_since + 1e-9 >= config.mismatch_delay
            ):
                state.active = True
                if config.locked:
                    state.locked = True
                return self._contactor_event(
                    "contactor_feedback_fault",
                    side,
                    name,
                )
            return None

        state.mismatch_since = None
        if not state.active and not state.locked:
            state.boot_verified = state.boot_verified or observed_closed is False
            return None

        if state.recovery_candidate_since is None:
            state.recovery_candidate_since = timestamp
        if (
            timestamp - state.recovery_candidate_since + 1e-9 < config.recovery_delay
            or state.recovery_ready
        ):
            return None

        state.active = False
        if state.locked:
            state.recovery_ready = True
            return self._contactor_event(
                "recovery_ready_manual_reset_required",
                side,
                name,
            )
        return self._contactor_event("protection_released", side, name)

    def _restart_contactor_recovery(self, state: _ContactorState) -> None:
        state.recovery_candidate_since = None
        state.recovery_ready = False

    def _contactor_reset_ready(self, state: _ContactorState) -> bool:
        if state.recovery_ready:
            return True
        if state.active or state.recovery_candidate_since is None:
            return False
        if self.current_timestamp is None:
            return False
        return (
            self.current_timestamp - state.recovery_candidate_since + 1e-9
            >= self.config.contactor_feedback.recovery_delay
        )

    def _contactor_event(
        self, event_type: str, side: str, name: str
    ) -> ProtectionEvent:
        state = self._contactor_states[side]
        config = self.config.contactor_feedback
        details = {
            "side": side,
            "commanded_closed": state.commanded_closed,
            "observed_closed": state.observed_closed,
            "mismatch_delay": config.mismatch_delay,
            "recovery_delay": config.recovery_delay,
            "locked": config.locked,
            "boot_verified": state.boot_verified,
        }
        return self._make_event(event_type, reasons=(name,), details=details)

    def _observe(self, measurement: Measurement) -> dict[str, bool]:
        states = {name: False for name in self._states}
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

        valid_voltages = [
            (index, float(value))
            for index, value in enumerate(voltages)
            if value is not None
        ]
        if valid_voltages:
            over_index, over_value = max(valid_voltages, key=lambda item: item[1])
            under_index, under_value = min(valid_voltages, key=lambda item: item[1])
            self._remember(
                "over_voltage", over_value, f"cell_{over_index + 1}_voltage"
            )
            self._remember(
                "under_voltage", under_value, f"cell_{under_index + 1}_voltage"
            )

        for index, value in enumerate(voltages):
            source = f"cell_{index + 1}_voltage"
            if value is None:
                missing_sources.append(source)
                continue
            if self._matches(self.config.over_voltage, float(value)):
                states["over_voltage"] = True
            if self._matches(self.config.under_voltage, float(value)):
                states["under_voltage"] = True

        valid_temperatures = [
            (index, float(value))
            for index, value in enumerate(temperatures)
            if value is not None
        ]
        if valid_temperatures:
            temperature_index, temperature_value = max(
                valid_temperatures, key=lambda item: item[1]
            )
            self._remember(
                "over_temperature",
                temperature_value,
                f"cell_{temperature_index + 1}_temperature",
            )

        for index, value in enumerate(temperatures):
            source = f"cell_{index + 1}_temperature"
            if value is None:
                missing_sources.append(source)
                continue
            if self._matches(self.config.over_temperature, float(value)):
                states["over_temperature"] = True

        measurement_state = self._states["measurement_fault"]
        if missing_sources:
            states["measurement_fault"] = True
            measurement_state.last_value = None
            measurement_state.source = ",".join(missing_sources)
        else:
            measurement_state.last_value = 1.0
            measurement_state.source = "all_measurements_valid"

        return states

    def _update_fault(
        self, name: str, observed: bool, timestamp: float
    ) -> ProtectionEvent | None:
        rule = getattr(self.config, name)
        state = self._states[name]

        if observed:
            if state.candidate_since is None:
                state.candidate_since = timestamp
            state.recovery_candidate_since = None
            state.recovery_ready = False
            if (
                not state.active
                and timestamp - state.candidate_since + 1e-9 >= rule.trigger_delay
            ):
                state.active = True
                if rule.locked:
                    state.locked = True
                return self._fault_event("protection_triggered", name)
            return None

        state.candidate_since = None
        if not state.active and not state.locked:
            return None

        if not self._matches_recovery(rule, state):
            state.recovery_candidate_since = None
            state.recovery_ready = False
            return None

        if state.recovery_candidate_since is None:
            state.recovery_candidate_since = timestamp
        if (
            timestamp - state.recovery_candidate_since + 1e-9
            < rule.recovery_delay
            or state.recovery_ready
        ):
            return None

        state.active = False
        if state.locked:
            state.recovery_ready = True
            return self._fault_event(
                "recovery_ready_manual_reset_required",
                name,
                extra_details={"action_remains": self._ACTIONS[name]},
            )

        state.recovery_ready = False
        return self._fault_event("protection_released", name)

    def _matches(self, rule: FaultRule, value: float) -> bool:
        threshold = rule.trigger_threshold
        if threshold is None:
            return False
        return value >= threshold if rule.direction == "above" else value <= threshold

    def _matches_recovery(self, rule: FaultRule, state: _FaultState) -> bool:
        threshold = rule.recovery_threshold
        if threshold is None:
            return state.last_value is not None
        value = state.last_value
        if value is None:
            return False
        return value <= threshold if rule.direction == "above" else value >= threshold

    def _reset_ready(
        self, name: str, timestamp: float | None = None
    ) -> bool:
        state = self._states[name]
        if state.recovery_ready:
            return True
        if state.active or state.recovery_candidate_since is None or timestamp is None:
            return False
        rule = getattr(self.config, name)
        return timestamp - state.recovery_candidate_since + 1e-9 >= rule.recovery_delay

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
        if self._contactor_states["charge"].active or self._contactor_states["charge"].locked:
            charge = True
        if self._contactor_states["discharge"].active or self._contactor_states["discharge"].locked:
            discharge = True
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
            )
            + tuple(
                name
                for side, name in self._CONTACTOR_SIDES
                if self._contactor_states[side].locked
            ),
            reasons=reasons,
            details=details,
            charge_feedback_closed=self._contactor_states["charge"].observed_closed,
            discharge_feedback_closed=self._contactor_states["discharge"].observed_closed,
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
        for side, name in self._CONTACTOR_SIDES:
            state = self._contactor_states[side]
            if not state.active and not state.locked:
                continue
            summary[name] = {
                "active": state.active,
                "locked": state.locked,
                "recovery_ready": state.recovery_ready,
                "recovery_delay": self.config.contactor_feedback.recovery_delay,
                "manual_reset_required": self.config.contactor_feedback.locked,
                "commanded_closed": state.commanded_closed,
                "observed_closed": state.observed_closed,
            }
        return summary
