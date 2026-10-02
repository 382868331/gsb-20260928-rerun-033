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
    post_open_verification: FaultRule = field(
        default_factory=lambda: FaultRule(0.5, None, 1.0, 0.5)
    )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProtectionConfig":
        rule_names = (
            "over_voltage",
            "under_voltage",
            "over_temperature",
            "measurement_fault",
            "contactor_mismatch",
            "post_open_verification",
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
    observed_closed: bool | None = None
    open_confirmed: bool = False
    mismatch: bool = False
    mismatch_since: float | None = None
    recovery_candidate_since: float | None = None


@dataclass
class _VerificationState:
    """Post-open verification: prove the bus really de-energised.

    elapsed banks confirmed de-energised observation time so a
    mid-verification re-close pauses the wait instead of resetting it.
    """

    required: bool = False
    verifying: bool = False
    passed: bool = False
    elapsed: float = 0.0
    good_since: float | None = None
    pending_reasons: list[str] = field(default_factory=list)
    last_bus_voltage: float | None = None


class ProtectionController:
    """Debounced, latching protection decision engine."""

    _ACTIONS: ClassVar[dict[str, str]] = {
        "over_voltage": "open_charge_contactor",
        "under_voltage": "open_discharge_contactor",
        "over_temperature": "open_both_contactors",
        "measurement_fault": "open_both_contactors",
        "charge_contactor_mismatch": "open_both_contactors",
        "discharge_contactor_mismatch": "open_both_contactors",
        "feedback_contradiction": "open_both_contactors",
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
                "feedback_contradiction",
            )
        }
        self._contactors = {
            "charge": _ContactorState(),
            "discharge": _ContactorState(),
        }
        self._verification = _VerificationState()
        self.events: list[ProtectionEvent] = []
        self.current_timestamp: float | None = None

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
    def disconnect_completed_by(self) -> tuple[str, ...]:
        """Poles that physically opened after being commanded open."""

        return tuple(
            name
            for name, contactor in self._contactors.items()
            if contactor.requested_closed is False and contactor.open_confirmed
        )

    @property
    def verification_status(self) -> dict[str, object]:
        verification = self._verification
        elapsed = verification.elapsed
        if (
            verification.good_since is not None
            and self.current_timestamp is not None
        ):
            elapsed += self.current_timestamp - verification.good_since
        return {
            "required": verification.required,
            "verifying": verification.verifying,
            "passed": verification.passed,
            "elapsed": elapsed,
            "pending_reasons": list(verification.pending_reasons),
            "last_bus_voltage": verification.last_bus_voltage,
            "repower_allowed": self.repower_allowed,
        }

    @property
    def repower_allowed(self) -> bool:
        verification = self._verification
        return not verification.required or verification.passed

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
        return tuple(
            name for name, state in self._states.items() if state.active or state.locked
        )

    @property
    def reset_ready_reasons(self) -> tuple[str, ...]:
        return tuple(
            name for name, state in self._states.items() if self._reset_ready(name)
        )

    @property
    def reset_blocked_reasons(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, state in self._states.items()
            if state.locked and not self._reset_ready(name)
        )

    def update(self, measurement: Measurement) -> list[ProtectionEvent]:
        before_charge = (
            self.charge_contactor_closed if self.events else True
        )
        before_discharge = (
            self.discharge_contactor_closed if self.events else True
        )
        self.current_timestamp = measurement.timestamp

        observations = self._observe(measurement)
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

        self._dispatch_contactor_commands()

        for contactor_name, observed in (
            ("charge", measurement.charge_contactor_feedback),
            ("discharge", measurement.discharge_contactor_feedback),
        ):
            produced.extend(
                self._update_contactor_feedback(
                contactor_name, observed, measurement.timestamp
                )
            )

        contradiction_event = self._update_fault(
            "feedback_contradiction",
            self._observe_contradiction(),
            measurement.timestamp,
        )
        if contradiction_event is not None:
            produced.append(contradiction_event)

        # A newly confirmed mismatch forces the opposite pole open as well.
        self._dispatch_contactor_commands()

        produced.extend(
            self._update_verification(measurement, measurement.timestamp)
        )

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
        verification = self._verification
        details["verification"] = self.verification_status
        if verification.verifying and not verification.passed:
            # Manual reset never substitutes for an incomplete verification.
            details["verification_blocking"] = True
            blocked = blocked + ["post_open_verification"]
        if blocked:
            details["blocked_reasons"] = blocked
            details["recovery_conditions"] = self._recovery_summary()
            event_type = "manual_reset" if reset_names else "reset_blocked"
        else:
            event_type = "manual_reset" if reset_names else "reset_idle"
        event = self._make_event(event_type, reasons=tuple(reset_names), details=details)
        self._dispatch_contactor_commands()
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
            "contactors": {
                name: {
                    "requested_closed": contactor.requested_closed,
                    "observed_closed": contactor.observed_closed,
                    "open_confirmed": contactor.open_confirmed,
                    "mismatch": contactor.mismatch,
                    "mismatch_since": contactor.mismatch_since,
                    "recovery_candidate_since": contactor.recovery_candidate_since,
                }
                for name, contactor in self._contactors.items()
            },
            "verification": {
                "required": self._verification.required,
                "verifying": self._verification.verifying,
                "passed": self._verification.passed,
                "elapsed": self._verification.elapsed,
                "good_since": self._verification.good_since,
                "pending_reasons": list(self._verification.pending_reasons),
                "last_bus_voltage": self._verification.last_bus_voltage,
            },
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
        contactors = snapshot.get("contactors")
        if isinstance(contactors, dict):
            for name, payload in contactors.items():
                assert isinstance(payload, dict)
                contactor = self._contactors[name]
                for field_name in (
                    "requested_closed",
                    "observed_closed",
                    "open_confirmed",
                    "mismatch",
                    "mismatch_since",
                    "recovery_candidate_since",
                ):
                    if field_name in payload:
                        setattr(contactor, field_name, payload[field_name])
        verification = snapshot.get("verification")
        if isinstance(verification, dict):
            target = self._verification
            for field_name in (
                "required",
                "verifying",
                "passed",
                "elapsed",
                "good_since",
                "last_bus_voltage",
            ):
                setattr(target, field_name, verification[field_name])
            target.pending_reasons = list(verification["pending_reasons"])

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
        rule = self._rule_for(name)
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
            "feedback_contradiction",
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
        return charge, discharge

    def _dispatch_contactor_commands(self) -> None:
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
            if requested_closed:
                contactor.open_confirmed = False
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
    ) -> list[ProtectionEvent]:
        contactor = self._contactors[contactor_name]
        contactor.observed_closed = observed_closed
        events: list[ProtectionEvent] = []
        if (
            contactor.requested_closed is False
            and observed_closed is False
            and not contactor.open_confirmed
        ):
            contactor.open_confirmed = True
            events.append(
                self._make_event(
                    "contactor_open_confirmed",
                    reasons=(),
                    details={
                        "contactor": contactor_name,
                        "requested_closed": False,
                        "observed_closed": False,
                        "disconnect_completed_by": contactor_name,
                    },
                )
            )
        if observed_closed is None:
            contactor.mismatch = False
            contactor.mismatch_since = None
            contactor.recovery_candidate_since = None
            return events

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
                events.append(
                    self._mismatch_event(
                        "protection_triggered",
                        mismatch_name,
                        contactor_name,
                        observed_closed,
                    )
                )
            return events

        contactor.mismatch = False
        contactor.mismatch_since = None
        if not state.active and not state.locked:
            return events

        state.candidate_since = None
        if contactor.recovery_candidate_since is None:
            contactor.recovery_candidate_since = timestamp
            state.recovery_candidate_since = timestamp
        if (
            timestamp - contactor.recovery_candidate_since + 1e-9
            < rule.recovery_delay
            or state.recovery_ready
        ):
            return events

        state.active = False
        contactor.recovery_candidate_since = None
        if state.locked:
            state.recovery_ready = True
            events.append(
                self._mismatch_event(
                    "recovery_ready_manual_reset_required",
                    mismatch_name,
                    contactor_name,
                    observed_closed,
                )
            )
            return events

        state.recovery_ready = False
        events.append(
            self._mismatch_event(
                "protection_released",
                mismatch_name,
                contactor_name,
                observed_closed,
            )
        )
        return events

    def _observe_contradiction(self) -> bool:
        """The two series poles report mutually exclusive physical states.

        Distinct from a per-pole mismatch: even if each pole is debounced
        against its own command, the pair of feedbacks cannot describe a
        sane series path while they disagree under a shared command.
        """

        charge = self._contactors["charge"]
        discharge = self._contactors["discharge"]
        state = self._states["feedback_contradiction"]
        if (
            charge.requested_closed is None
            or charge.requested_closed != discharge.requested_closed
            or charge.observed_closed is None
            or discharge.observed_closed is None
        ):
            state.last_value = None
            state.source = "contactor_feedback_unavailable"
            return False
        contradictory = charge.observed_closed != discharge.observed_closed
        state.last_value = 1.0 if contradictory else 0.0
        state.source = (
            "charge_contactor_feedback="
            f"{charge.observed_closed},"
            f" discharge_contactor_feedback={discharge.observed_closed}"
        )
        return contradictory

    def _update_verification(
        self, measurement: Measurement, timestamp: float
    ) -> list[ProtectionEvent]:
        verification = self._verification
        rule = self.config.post_open_verification
        events: list[ProtectionEvent] = []
        both_requested_open = all(
            contactor.requested_closed is False
            for contactor in self._contactors.values()
        )
        both_observed_open = all(
            contactor.observed_closed is False
            for contactor in self._contactors.values()
        )

        if not both_requested_open:
            # Re-closed (or never opened): pause the wait, keep banked time.
            self._bank_good_time(timestamp)
            if verification.passed:
                self._verification = _VerificationState()
            return events

        verification.required = True
        if not both_observed_open:
            self._bank_good_time(timestamp)
            return events

        if not verification.verifying:
            verification.verifying = True
            events.append(
                self._verification_event(
                    "verification_started",
                    {"both_contactors_reported_open": True},
                )
            )

        bus_voltage = measurement.bus_voltage
        verification.last_bus_voltage = bus_voltage
        threshold = rule.trigger_threshold
        if bus_voltage is None:
            self._bank_good_time(timestamp)
            events.extend(
                self._add_verification_reason("observation_missing")
            )
            return events
        if threshold is not None and bus_voltage >= threshold:
            had_good = (
                verification.good_since is not None
                or verification.elapsed > 0.0
            )
            self._bank_good_time(timestamp)
            reason = "brief_recovery" if had_good else "voltage_not_decayed"
            events.extend(self._add_verification_reason(reason))
            return events

        if verification.good_since is None:
            verification.good_since = timestamp
        total = verification.elapsed + (timestamp - verification.good_since)
        if not verification.passed and total + 1e-9 >= rule.trigger_delay:
            verification.passed = True
            cleared = list(verification.pending_reasons)
            verification.pending_reasons = []
            events.append(
                self._verification_event(
                    "verification_passed",
                    {
                        "observed_bus_voltage": bus_voltage,
                        "voltage_threshold": threshold,
                        "required_delay": rule.trigger_delay,
                        "observed_delay": total,
                        "cleared_reasons": cleared,
                        "repower_allowed": True,
                    },
                )
            )
        return events

    def _bank_good_time(self, timestamp: float) -> None:
        verification = self._verification
        if verification.good_since is not None:
            verification.elapsed += timestamp - verification.good_since
            verification.good_since = None

    def _add_verification_reason(self, reason: str) -> list[ProtectionEvent]:
        verification = self._verification
        if reason in verification.pending_reasons:
            return []
        verification.pending_reasons.append(reason)
        return [
            self._verification_event(
                "verification_pending",
                {
                    "reason": reason,
                    "pending_reasons": list(verification.pending_reasons),
                    "last_bus_voltage": verification.last_bus_voltage,
                    "voltage_threshold": (
                        self.config.post_open_verification.trigger_threshold
                    ),
                },
            )
        ]

    def _verification_event(
        self, event_type: str, extra_details: dict[str, object]
    ) -> ProtectionEvent:
        details: dict[str, object] = {
            "action": "hold_open_until_verified",
            "source": "bus_voltage",
        }
        details.update(extra_details)
        return self._make_event(
            event_type, reasons=("post_open_verification",), details=details
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
        rule = self._rule_for(name)
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
            rule = self._rule_for(name)
            summary[name] = {
                "active": state.active,
                "locked": state.locked,
                "recovery_ready": state.recovery_ready,
                "recovery_threshold": rule.recovery_threshold,
                "recovery_delay": rule.recovery_delay,
                "manual_reset_required": rule.locked,
            }
        return summary
