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
    contactor_mismatch: FaultRule = field(
        default_factory=lambda: FaultRule(None, None, 0.5, 1.0)
    )
    post_disconnect_voltage: FaultRule = field(
        default_factory=lambda: FaultRule(5.0, 5.0, 0.0, 0.5)
    )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProtectionConfig":
        rule_names = (
            "over_voltage",
            "under_voltage",
            "over_temperature",
            "measurement_fault",
            "contactor_mismatch",
            "post_disconnect_voltage",
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
    requested_closed: bool | None = None
    requested_since: float | None = None
    observed_closed: bool | None = None
    observed_since: float | None = None
    mismatch: bool = False
    mismatch_since: float | None = None
    recovery_candidate_since: float | None = None


@dataclass
class _DisconnectVerification:
    required: bool = False
    both_open_since: float | None = None
    low_voltage_since: float | None = None
    active: bool = False
    locked: bool = False
    recovery_ready: bool = False
    failure_reason: str = ""
    last_voltage: float | None = None
    actual_disconnect_contactor: str | None = None
    last_opened_contactor: str | None = None
    low_voltage_observed: bool = False


class ProtectionController:
    """Debounced, latching protection decision engine."""

    _ACTIONS: ClassVar[dict[str, str]] = {
        "over_voltage": "open_charge_contactor",
        "under_voltage": "open_discharge_contactor",
        "over_temperature": "open_both_contactors",
        "measurement_fault": "open_both_contactors",
        "charge_contactor_mismatch": "open_both_contactors",
        "discharge_contactor_mismatch": "open_both_contactors",
        "post_disconnect_voltage_verification": "open_both_contactors",
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
                "charge_contactor_mismatch",
                "discharge_contactor_mismatch",
            )
        }
        self._contactors = {
            "charge": _ContactorState(),
            "discharge": _ContactorState(),
        }
        self._disconnect_verification = _DisconnectVerification()
        self.events: list[ProtectionEvent] = []
        self.current_timestamp: float | None = None
        self.current_load_voltage: float | None = None

    @property
    def charge_contactor_closed(self) -> bool:
        return not self._contactors_blocked()[0]

    @property
    def discharge_contactor_closed(self) -> bool:
        return not self._contactors_blocked()[1]

    @property
    def charge_contactor_observed_closed(self) -> bool | None:
        return self._contactors["charge"].observed_closed

    @property
    def discharge_contactor_observed_closed(self) -> bool | None:
        return self._contactors["discharge"].observed_closed

    @property
    def post_disconnect_verification(self) -> dict[str, object]:
        verification = self._disconnect_verification
        return {
            "required": verification.required,
            "active": verification.active,
            "locked": verification.locked,
            "recovery_ready": verification.recovery_ready,
            "both_open_since": verification.both_open_since,
            "low_voltage_since": verification.low_voltage_since,
            "failure_reason": verification.failure_reason,
            "last_voltage": verification.last_voltage,
            "actual_disconnect_contactor": (
                verification.actual_disconnect_contactor
            ),
            "last_opened_contactor": verification.last_opened_contactor,
            "low_voltage_observed": verification.low_voltage_observed,
        }

    @property
    def contactor_mismatch_reasons(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, state in self._states.items()
            if name.endswith("_contactor_mismatch")
            and (state.active or state.locked)
        )

    @property
    def active_reasons(self) -> tuple[str, ...]:
        reasons = [
            name for name, state in self._states.items() if state.active or state.locked
        ]
        verification = self._disconnect_verification
        if verification.active or verification.locked:
            reasons.append("post_disconnect_voltage_verification")
        return tuple(reasons)

    @property
    def reset_ready_reasons(self) -> tuple[str, ...]:
        reasons = [
            name for name, state in self._states.items() if self._reset_ready(name)
        ]
        if (
            self._disconnect_verification.locked
            and self._disconnect_verification.recovery_ready
        ):
            reasons.append("post_disconnect_voltage_verification")
        return tuple(reasons)

    @property
    def reset_blocked_reasons(self) -> tuple[str, ...]:
        reasons = [
            name
            for name, state in self._states.items()
            if state.locked and not self._reset_ready(name)
        ]
        verification = self._disconnect_verification
        if verification.required and (verification.active or verification.locked) and not verification.recovery_ready:
            reasons.append("post_disconnect_voltage_verification")
        return tuple(reasons)

    def update(self, measurement: Measurement) -> list[ProtectionEvent]:
        before_charge = (
            self.charge_contactor_closed if self.events else True
        )
        before_discharge = (
            self.discharge_contactor_closed if self.events else True
        )
        self.current_timestamp = measurement.timestamp

        observations = self._observe(measurement)
        self.current_load_voltage = measurement.load_voltage
        produced: list[ProtectionEvent] = []
        for name in (
            "over_voltage",
            "under_voltage",
            "over_temperature",
            "measurement_fault",
        ):
            observed = observations[name]
            event = self._update_fault(name, observed, measurement.timestamp)
            if event is not None:
                produced.append(event)

        self._dispatch_contactor_commands(measurement.timestamp)

        for contactor_name, observed in (
            ("charge", measurement.charge_contactor_feedback),
            ("discharge", measurement.discharge_contactor_feedback),
        ):
            event = self._update_contactor_feedback(
                contactor_name, observed, measurement.timestamp
            )
            if event is not None:
                produced.append(event)

        # A newly confirmed mismatch forces the opposite pole open as well.
        self._dispatch_contactor_commands(measurement.timestamp)

        verification_events = self._update_disconnect_verification(measurement)
        produced.extend(verification_events)

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
        verification = self._disconnect_verification
        if (
            verification.required
            and (verification.active or verification.locked)
            and not verification.recovery_ready
        ):
            blocked.append("post_disconnect_voltage_verification")
        elif verification.locked and verification.recovery_ready:
            verification.required = False
            verification.active = False
            verification.locked = False
            verification.recovery_ready = False
            verification.both_open_since = None
            verification.low_voltage_since = None
            verification.low_voltage_observed = False
            verification.failure_reason = ""
            reset_names.append("post_disconnect_voltage_verification")
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
            if name in {
                "charge_contactor_mismatch",
                "discharge_contactor_mismatch",
            }:
                contactor_name = name.removesuffix("_contactor_mismatch")
                contactor = self._contactors[contactor_name]
                contactor.mismatch = False
                contactor.mismatch_since = None
                contactor.recovery_candidate_since = None
            reset_names.append(name)

        details: dict[str, object] = {"reset_reasons": reset_names}
        if blocked:
            details["blocked_reasons"] = blocked
            details["recovery_conditions"] = self._recovery_summary()
            event_type = "manual_reset" if reset_names else "reset_blocked"
        else:
            event_type = "manual_reset" if reset_names else "reset_idle"
        event = self._make_event(event_type, reasons=tuple(reset_names), details=details)
        self._dispatch_contactor_commands(reset_timestamp)
        self.events.append(event)
        return [event]

    def replay(self) -> list[ProtectionEvent]:
        return list(self.events)

    def state_snapshot(self) -> dict[str, object]:
        verification = self._disconnect_verification
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
            "contactors": {
                name: {
                    "requested_closed": contactor.requested_closed,
                    "requested_since": contactor.requested_since,
                    "observed_closed": contactor.observed_closed,
                    "observed_since": contactor.observed_since,
                    "mismatch": contactor.mismatch,
                    "mismatch_since": contactor.mismatch_since,
                    "recovery_candidate_since": contactor.recovery_candidate_since,
                }
                for name, contactor in self._contactors.items()
            },
            "disconnect_verification": {
                "required": verification.required,
                "both_open_since": verification.both_open_since,
                "low_voltage_since": verification.low_voltage_since,
                "active": verification.active,
                "locked": verification.locked,
                "recovery_ready": verification.recovery_ready,
                "failure_reason": verification.failure_reason,
                "last_voltage": verification.last_voltage,
                "actual_disconnect_contactor": (
                    verification.actual_disconnect_contactor
                ),
                "last_opened_contactor": verification.last_opened_contactor,
                "low_voltage_observed": verification.low_voltage_observed,
            },
        }

    def restore_state(self, snapshot: dict[str, object]) -> None:
        self.current_timestamp = snapshot["timestamp"]  # type: ignore[assignment]
        verification = self._disconnect_verification
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
        contactors = snapshot.get("contactors")
        if isinstance(contactors, dict):
            for name, payload in contactors.items():
                assert isinstance(payload, dict)
                contactor = self._contactors[name]
                for field_name in (
                    "requested_closed",
                    "requested_since",
                    "observed_closed",
                    "observed_since",
                    "mismatch",
                    "mismatch_since",
                    "recovery_candidate_since",
                ):
                    setattr(contactor, field_name, payload[field_name])
        verification_payload = snapshot.get("disconnect_verification")
        if isinstance(verification_payload, dict):
            for field_name in (
                "required",
                "both_open_since",
                "low_voltage_since",
                "active",
                "locked",
                "recovery_ready",
                "failure_reason",
                "last_voltage",
                "actual_disconnect_contactor",
                "last_opened_contactor",
                "low_voltage_observed",
            ):
                setattr(
                    verification,
                    field_name,
                    verification_payload.get(field_name, getattr(verification, field_name)),
                )

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
        rule = self._rule_for(name)
        return timestamp - state.recovery_candidate_since + 1e-9 >= rule.recovery_delay

    def _rule_for(self, name: str) -> FaultRule:
        if name in {
            "charge_contactor_mismatch",
            "discharge_contactor_mismatch",
        }:
            return self.config.contactor_mismatch
        return getattr(self.config, name)

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
        if self._disconnect_verification.active or self._disconnect_verification.locked:
            charge = True
            discharge = True
        return charge, discharge

    def _dispatch_contactor_commands(self, timestamp: float | None = None) -> None:
        """Issue latest requested open/closed commands to each contactor.

        A changed command invalidates any in-progress mismatch confirmation
        or recovery timing: the wait must start from the new command.
        """

        block_charge, block_discharge = self._contactors_blocked()
        for contactor_name, requested_closed in (
            ("charge", not block_charge),
            ("discharge", not block_discharge),
        ):
            contactor = self._contactors[contactor_name]
            if contactor.requested_closed == requested_closed:
                continue
            contactor.requested_closed = requested_closed
            contactor.requested_since = timestamp
            contactor.mismatch = False
            contactor.mismatch_since = None
            contactor.recovery_candidate_since = None
            mismatch_state = self._states[
                f"{contactor_name}_contactor_mismatch"
            ]
            mismatch_state.recovery_candidate_since = None

    def _update_contactor_feedback(
        self,
        contactor_name: str,
        observed_closed: bool | None,
        timestamp: float,
    ) -> ProtectionEvent | None:
        contactor = self._contactors[contactor_name]
        previous_observed = contactor.observed_closed
        contactor.observed_closed = observed_closed
        if observed_closed != previous_observed:
            contactor.observed_since = timestamp
            if observed_closed is False:
                self._disconnect_verification.last_opened_contactor = contactor_name
        if observed_closed is None:
            contactor.mismatch = False
            contactor.mismatch_since = None
            contactor.recovery_candidate_since = None
            return None

        rule = self.config.contactor_mismatch
        mismatch_name = f"{contactor_name}_contactor_mismatch"
        state = self._states[mismatch_name]
        state.last_value = 1.0 if observed_closed else 0.0
        state.source = (
            f"{contactor_name}_contactor_feedback"
            f"(requested={contactor.requested_closed},"
            f" observed={observed_closed})"
        )

        differs = observed_closed != contactor.requested_closed
        if differs:
            contactor.mismatch = True
            if contactor.mismatch_since is None:
                contactor.mismatch_since = timestamp
            contactor.recovery_candidate_since = None
            state.recovery_candidate_since = None
            state.recovery_ready = False
            if (
                not state.active
                and timestamp - contactor.mismatch_since + 1e-9
                >= rule.trigger_delay
            ):
                state.active = True
                if rule.locked:
                    state.locked = True
                return self._mismatch_event(
                    "protection_triggered",
                    mismatch_name,
                    contactor_name,
                    observed_closed,
                )
            return None

        contactor.mismatch = False
        contactor.mismatch_since = None
        if not state.active and not state.locked:
            return None

        state.candidate_since = None
        if contactor.recovery_candidate_since is None:
            contactor.recovery_candidate_since = timestamp
            state.recovery_candidate_since = timestamp
        if (
            timestamp - contactor.recovery_candidate_since + 1e-9
            < rule.recovery_delay
            or state.recovery_ready
        ):
            return None

        state.active = False
        contactor.recovery_candidate_since = None
        if state.locked:
            state.recovery_ready = True
            return self._mismatch_event(
                "recovery_ready_manual_reset_required",
                mismatch_name,
                contactor_name,
                observed_closed,
            )

        state.recovery_ready = False
        return self._mismatch_event(
            "protection_released",
            mismatch_name,
            contactor_name,
            observed_closed,
        )

    def _update_disconnect_verification(
        self, measurement: Measurement
    ) -> list[ProtectionEvent]:
        verification = self._disconnect_verification
        if not (
            self._contactors["charge"].requested_closed is False
            and self._contactors["discharge"].requested_closed is False
        ):
            return []

        verification.required = True
        verification.active = True
        timestamp = measurement.timestamp
        charge_open = measurement.charge_contactor_feedback is False
        discharge_open = measurement.discharge_contactor_feedback is False
        voltage = measurement.load_voltage
        verification.last_voltage = voltage
        events: list[ProtectionEvent] = []

        if charge_open and discharge_open:
            charge_since = self._contactors["charge"].observed_since
            discharge_since = self._contactors["discharge"].observed_since
            both_open_since = max(
                value for value in (charge_since, discharge_since) if value is not None
            )
            if verification.both_open_since is None:
                verification.both_open_since = both_open_since
                verification.failure_reason = ""
                events.append(
                    self._verification_event(
                        "post_disconnect_verification_started",
                        reason="waiting_for_sustained_low_voltage",
                    )
                )
        else:
            closed_names = [
                name
                for name in ("charge", "discharge")
                if self._contactors[name].observed_closed is not False
            ]
            verification.failure_reason = "waiting_for_contactor_feedback:"
            verification.failure_reason += ",".join(closed_names) or "missing_feedback"

        if not charge_open or not discharge_open:
            verification.low_voltage_since = None
        elif voltage is None:
            verification.low_voltage_since = None
            verification.locked = True
            events.append(
                self._verification_failure_event(
                    "load_voltage_observation_missing"
                )
            )
        elif voltage >= self.config.post_disconnect_voltage.recovery_threshold:
            had_low_voltage = (
                verification.low_voltage_since is not None
                or verification.low_voltage_observed
            )
            reason = (
                "voltage_recovered"
                if had_low_voltage
                else "open_feedback_contradicts_voltage"
            )
            verification.low_voltage_since = None
            verification.locked = True
            events.append(
                self._verification_failure_event(reason)
            )
        else:
            verification.low_voltage_observed = True
            if verification.low_voltage_since is None:
                verification.low_voltage_since = timestamp
            if (
                timestamp - verification.low_voltage_since + 1e-9
                >= self.config.post_disconnect_voltage.recovery_delay
            ):
                previous_reason = verification.failure_reason
                verification.active = False
                verification.locked = True
                verification.recovery_ready = True
                verification.failure_reason = ""
                charge_since = self._contactors["charge"].observed_since
                discharge_since = self._contactors["discharge"].observed_since
                verification.actual_disconnect_contactor = "both"
                if charge_since is not None and discharge_since is not None:
                    if charge_since < discharge_since:
                        verification.actual_disconnect_contactor = "discharge"
                    elif discharge_since < charge_since:
                        verification.actual_disconnect_contactor = "charge"
                event = self._verification_event(
                    "post_disconnect_verification_passed",
                    reason="sustained_low_voltage",
                )
                if previous_reason:
                    event.details["previous_failure_reason"] = previous_reason
                events.append(event)

        return events

    def _verification_failure_event(
        self, reason: str
    ) -> ProtectionEvent | None:
        verification = self._disconnect_verification
        if verification.failure_reason == reason and not verification.recovery_ready:
            return None
        verification.failure_reason = reason
        verification.recovery_ready = False
        return self._verification_event(
            "post_disconnect_verification_failed",
            reason=reason,
        )

    def _verification_event(
        self, event_type: str, reason: str
    ) -> ProtectionEvent:
        verification = self._disconnect_verification
        rule = self.config.post_disconnect_voltage
        details = {
            "source": "load_voltage_and_contactor_feedback",
            "failure_reason": verification.failure_reason or reason,
            "load_voltage": verification.last_voltage,
            "safe_voltage_threshold": rule.recovery_threshold,
            "required_low_voltage_duration": rule.recovery_delay,
            "both_open_since": verification.both_open_since,
            "low_voltage_since": verification.low_voltage_since,
            "actual_disconnect_contactor": (
                verification.actual_disconnect_contactor
            ),
            "charge_feedback": (
                self._contactors["charge"].observed_closed
            ),
            "discharge_feedback": (
                self._contactors["discharge"].observed_closed
            ),
        }
        return self._make_event(
            event_type,
            reasons=("post_disconnect_voltage_verification",),
            details=details,
        )

    def _mismatch_event(
        self,
        event_type: str,
        name: str,
        contactor_name: str,
        observed_closed: bool,
    ) -> ProtectionEvent:
        contactor = self._contactors[contactor_name]
        rule = self.config.contactor_mismatch
        details = {
            "action": self._ACTIONS[name],
            "source": f"{contactor_name}_contactor_feedback",
            "requested_closed": contactor.requested_closed,
            "observed_closed": observed_closed,
            "trigger_threshold": rule.trigger_threshold,
            "recovery_threshold": rule.recovery_threshold,
            "trigger_delay": rule.trigger_delay,
            "recovery_delay": rule.recovery_delay,
            "locked": rule.locked,
        }
        return self._make_event(event_type, reasons=(name,), details=details)

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
                [name for name, state in self._states.items() if state.locked]
                + (
                    ["post_disconnect_voltage_verification"]
                    if self._disconnect_verification.locked
                    else []
                )
            ),
            reasons=reasons,
            details={
                **details,
                "charge_command_closed": self.charge_contactor_closed,
                "discharge_command_closed": self.discharge_contactor_closed,
                "charge_feedback_closed": (
                    self._contactors["charge"].observed_closed
                ),
                "discharge_feedback_closed": (
                    self._contactors["discharge"].observed_closed
                ),
                "load_voltage": self.current_load_voltage,
            },
        )

    def _recovery_summary(self) -> dict[str, object]:
        summary = {}
        for name, state in self._states.items():
            if not state.active and not state.locked:
                continue
            rule = self._rule_for(name)
            summary[name] = {
                "active": state.active,
                "locked": state.locked,
                "recovery_ready": state.recovery_ready,
                "recovery_threshold": rule.recovery_threshold,
                "recovery_delay": rule.recovery_delay,
                "manual_reset_required": rule.locked,
            }
        verification = self._disconnect_verification
        if verification.required:
            rule = self.config.post_disconnect_voltage
            summary["post_disconnect_voltage_verification"] = {
                "active": verification.active,
                "locked": verification.locked,
                "recovery_ready": verification.recovery_ready,
                "recovery_threshold": rule.recovery_threshold,
                "recovery_delay": rule.recovery_delay,
                "manual_reset_required": True,
                "failure_reason": verification.failure_reason,
                "actual_disconnect_contactor": (
                    verification.actual_disconnect_contactor
                ),
            }
        return summary
