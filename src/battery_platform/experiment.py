from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .models import Measurement, ProtectionEvent
from .protection import ProtectionConfig, ProtectionController
from .recorder import JsonlEventRecorder
from .simulator import VirtualBatteryPack, VirtualContactor


@dataclass(frozen=True)
class ExperimentScenario:
    """One protection experiment: actuation order plus fault injection.

    - charge_actuation_delay / discharge_actuation_delay set how slowly
      each pole physically follows a command, i.e. the actuation order
      the user wants to compare.
    - stuck injects a welded/stuck pole: "charge" or "discharge".
    - bus_voltage_fn overrides the bus voltage observation; by default
      the bus shows the pack voltage only while both poles are physically
      closed, otherwise 0.0.
    """

    name: str = "baseline"
    fault_kind: str = "over_temperature"
    charge_actuation_delay: float = 0.1
    discharge_actuation_delay: float = 0.3
    stuck: str | None = None
    over_voltage_cell: int = 0
    fault_start: float = 0.5
    duration: float = 6.0
    time_step: float = 0.1
    bus_voltage_fn: Callable[[float, bool, bool], float | None] | None = None


@dataclass
class ExperimentResult:
    scenario: ExperimentScenario
    events: list[ProtectionEvent] = field(default_factory=list)
    trace: list[dict[str, object]] = field(default_factory=list)


def run_experiment(
    scenario: ExperimentScenario,
    config: ProtectionConfig | None = None,
    recorder: JsonlEventRecorder | None = None,
) -> ExperimentResult:
    """Drive controller + virtual contactors through one scenario.

    Every sample records the issued command, the physical feedback and the
    bus voltage evidence, so two scenarios can be diffed along the record.
    """

    controller = ProtectionController(config)
    pack = VirtualBatteryPack(time_step=scenario.time_step)
    charge = VirtualContactor(
        name="charge",
        actuation_delay=scenario.charge_actuation_delay,
        stuck=scenario.stuck == "charge",
    )
    discharge = VirtualContactor(
        name="discharge",
        actuation_delay=scenario.discharge_actuation_delay,
        stuck=scenario.stuck == "discharge",
    )
    result = ExperimentResult(scenario=scenario)

    timestamp = 0.0
    while timestamp < scenario.duration - 1e-9:
        charge.apply_command(controller.charge_contactor_closed, timestamp)
        discharge.apply_command(
            controller.discharge_contactor_closed, timestamp
        )
        charge_feedback = charge.observe(timestamp)
        discharge_feedback = discharge.observe(timestamp)
        if scenario.bus_voltage_fn is not None:
            bus_voltage = scenario.bus_voltage_fn(
                timestamp, charge_feedback, discharge_feedback
            )
        else:
            both_closed = charge_feedback and discharge_feedback
            bus_voltage = (
                pack.nominal_voltage * pack.cell_count if both_closed else 0.0
            )

        def voltage(index: int, t: float) -> float:
            if (
                scenario.fault_kind == "over_voltage"
                and index == scenario.over_voltage_cell
                and t >= scenario.fault_start
            ):
                return 4.30
            return pack.nominal_voltage

        def temperature(index: int, t: float) -> float:
            if scenario.fault_kind == "over_temperature" and (
                t >= scenario.fault_start
            ):
                return 60.0
            return pack.nominal_temperature

        sample = pack.sample(
            voltage_fn=voltage,
            temperature_fn=temperature,
            charge_feedback=charge_feedback,
            discharge_feedback=discharge_feedback,
        )
        measurement = Measurement(
            timestamp=sample.timestamp,
            voltages=sample.voltages,
            temperatures=sample.temperatures,
            charge_contactor_feedback=sample.charge_contactor_feedback,
            discharge_contactor_feedback=sample.discharge_contactor_feedback,
            bus_voltage=bus_voltage,
        )
        if recorder is not None:
            recorder.record_measurement(measurement)
        events = controller.update(measurement)
        if recorder is not None:
            recorder.record_events(events)
            recorder.record_controller_state(controller)
        result.events.extend(events)
        result.trace.append(
            {
                "timestamp": timestamp,
                "charge_requested_closed": controller.charge_contactor_closed,
                "discharge_requested_closed": (
                    controller.discharge_contactor_closed
                ),
                "charge_feedback": charge_feedback,
                "discharge_feedback": discharge_feedback,
                "bus_voltage": bus_voltage,
                "disconnect_completed_by": controller.disconnect_completed_by,
                "active_reasons": controller.active_reasons,
                "verification": controller.verification_status,
            }
        )
        timestamp += scenario.time_step

    return result
