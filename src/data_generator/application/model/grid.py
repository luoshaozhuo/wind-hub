"""Single-bus three-phase grid equivalent with explicit frequency modes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from data_generator.application.model.common import (
    calculate_apparent_quantities,
    validate_finite_outputs,
    require_number,
    validate_positive_parameters,
)
from data_generator.domain.model import GeneratorError


class Grid:
    def __init__(self, params: Mapping[str, object]) -> None:
        self.p = params
        validate_positive_parameters(params, "nominal_voltage_v", "source_voltage_v", "nominal_frequency_hz")
        self.r = require_number(params, "grid_r_ohm", minimum=0)
        self.x = require_number(params, "grid_x_ohm", minimum=0)
        self.mode = str(params.get("frequency_mode", "external"))
        if self.mode not in {"external", "dynamic"}:
            raise GeneratorError("invalid grid frequency mode")
        if self.mode == "dynamic":
            validate_positive_parameters(params, "equivalent_inertia_s", "frequency_base_power_w")
            require_number(params, "frequency_damping_w_hz", minimum=0)
        self.frequency = require_number(params, "nominal_frequency_hz")

    def advance(
        self,
        dt: float,
        participants: Sequence[Mapping[str, float | int]],
        controls: Mapping[str, float],
    ) -> dict[str, float | int]:
        p = sum(float(point.get("active_power", 0.0)) for point in participants)
        q = sum(float(point.get("reactive_power", 0.0)) for point in participants)
        nominal_voltage = require_number(self.p, "nominal_voltage_v")
        source_voltage = float(
            controls.get("grid_voltage_source", require_number(self.p, "source_voltage_v"))
        )
        connected = int(controls.get("grid_connected", 1))
        if connected not in (0, 1) or source_voltage <= 0:
            raise GeneratorError("invalid grid voltage or connection command")
        voltage = source_voltage + (self.r * p + self.x * q) / nominal_voltage if connected else 0.0
        if self.mode == "external":
            frequency = float(
                controls.get("grid_frequency_input", require_number(self.p, "nominal_frequency_hz"))
            )
        else:
            imbalance = float(controls.get("power_imbalance_w", 0.0))
            nominal_frequency = require_number(self.p, "nominal_frequency_hz")
            frequency = self.frequency + dt * nominal_frequency * (
                imbalance
                - require_number(self.p, "frequency_damping_w_hz") * (self.frequency - nominal_frequency)
            ) / (
                2
                * require_number(self.p, "equivalent_inertia_s")
                * require_number(self.p, "frequency_base_power_w")
            )
        if frequency <= 0:
            raise GeneratorError("grid frequency must be positive")
        rocof = (frequency - self.frequency) / dt
        self.frequency = frequency
        if connected and voltage > 0:
            apparent_power, current, pf = calculate_apparent_quantities(p, q, voltage)
        else:
            apparent_power, current, pf = 0.0, 0.0, 1.0
        return validate_finite_outputs(
            {
                "grid_frequency": frequency,
                "frequency_deviation": frequency - require_number(self.p, "nominal_frequency_hz"),
                "frequency_rocof": rocof,
                "grid_voltage": voltage,
                "voltage_deviation": voltage - nominal_voltage,
                "grid_current": current,
                "sim_apparent_power": apparent_power,
                "active_power": p if connected else 0.0,
                "reactive_power": q if connected else 0.0,
                "sim_power_factor": pf,
                "grid_status": connected,
                "fault_status": int(not connected),
                "grid_frequency_input": frequency,
                "grid_voltage_source": source_voltage,
                "grid_connected": connected,
            }
        )
