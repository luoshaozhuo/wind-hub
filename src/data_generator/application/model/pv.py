"""Single-peak PV array, MPPT lag and AC inverter capacity model."""

from __future__ import annotations

import math
from collections.abc import Mapping

from data_generator.application.model.common import (
    apparent,
    clamp,
    finite_outputs,
    number,
    positive,
    response,
)
from data_generator.application.model.weather import Weather
from data_generator.domain.model import GeneratorError


class PV:
    def __init__(self, params: Mapping[str, object]) -> None:
        self.p = params
        positive(params, "array_peak_power_w", "inverter_rated_power_w",
                 "inverter_rated_apparent_power_va", "stc_irradiance_w_m2",
                 "mppt_voltage_v", "open_circuit_voltage_v", "mppt_tau_s",
                 "thermal_tau_s", "inverter_efficiency")
        if (number(params, "inverter_rated_power_w") > number(params, "inverter_rated_apparent_power_va")
                or number(params, "mppt_voltage_v") >= number(params, "open_circuit_voltage_v")
                or number(params, "inverter_efficiency") > 1):
            raise GeneratorError("invalid PV nominal capacity or MPPT voltage")
        self.cell_temperature = number(params, "stc_cell_temperature_c")
        self.dc_voltage = number(params, "mppt_voltage_v")
        self.inverter_temperature = self.cell_temperature
        enabled = params.get("mppt_enabled", True)
        if type(enabled) is not bool:
            raise GeneratorError("PV MPPT enable must be Boolean")
        self.mppt_enabled = enabled
        self.dc_target = (
            number(params, "mppt_voltage_v") if enabled
            else number(params, "dc_voltage_setpoint", minimum=0)
        )
        if not 0 < self.dc_target < number(params, "open_circuit_voltage_v"):
            raise GeneratorError("PV target DC voltage must be within open-circuit voltage")
        number(params, "power_temp_coefficient_per_k")

    def advance(
        self, dt: float, weather: Weather, controls: Mapping[str, float], voltage: float
    ) -> dict[str, float | int]:
        p = self.p
        g = weather.irradiance
        noct = number(p, "noct_c")
        target_temperature = weather.ambient_temperature + (noct - 20) * g / 800
        self.cell_temperature = response(self.cell_temperature, target_temperature, dt, number(p, "thermal_tau_s"))
        available = max(0.0, number(p, "array_peak_power_w") * g / number(p, "stc_irradiance_w_m2")
                        * (1 + number(p, "power_temp_coefficient_per_k")
                           * (self.cell_temperature - number(p, "stc_cell_temperature_c"))))
        mp = number(p, "mppt_voltage_v")
        oc = number(p, "open_circuit_voltage_v")
        self.dc_voltage = response(
            self.dc_voltage, self.dc_target, dt, number(p, "mppt_tau_s")
        )
        if self.dc_voltage < mp:
            shape = max(0, self.dc_voltage) / mp
        else:
            shape = clamp((oc - self.dc_voltage) / (oc - mp), 0, 1)
        dc_available = available * shape
        rated = number(p, "inverter_rated_power_w")
        command = clamp(float(controls.get("active_power_setpoint", rated)), 0, rated)
        eta = number(p, "inverter_efficiency")
        power = (
            min(eta * dc_available, rated, command)
            if bool(controls.get("_grid_connected", 1)) else 0.0
        )
        # Curtail upstream extraction; unused irradiance is not inverter heat.
        dc_power = power / eta
        dc_current = dc_power / self.dc_voltage if self.dc_voltage > 0 else 0.0
        qcap = math.sqrt(max(0, number(p, "inverter_rated_apparent_power_va")**2 - power**2))
        reactive = (
            clamp(float(controls.get("reactive_power_setpoint", 0)), -qcap, qcap)
            if g > 0 and bool(controls.get("_grid_connected", 1)) else 0.0
        )
        _, current, pf = apparent(power, reactive, voltage)
        loss = max(0, dc_power - power)
        self.inverter_temperature = response(
            self.inverter_temperature, weather.ambient_temperature + loss / 1000,
            dt, number(p, "thermal_tau_s"),
        )
        return finite_outputs({
            "cell_temperature": self.cell_temperature,
            "pv_available_power": available,
            "mppt_state": int(self.mppt_enabled),
            "dc_voltage": self.dc_voltage,
            "dc_current": dc_current,
            "dc_power": dc_power,
            "active_power": power,
            "reactive_power": reactive,
            "ac_current": current,
            "sim_power_factor": pf,
            "inverter_temperature": self.inverter_temperature,
            "inverter_status": int(g > 0 and bool(controls.get("_grid_connected", 1))),
            "fault_status": 0,
            "control_result": 0,
            "active_power_setpoint": command,
            "reactive_power_setpoint": reactive,
        })
