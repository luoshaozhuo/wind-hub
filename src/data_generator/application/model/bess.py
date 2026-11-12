"""Battery chemical-energy model with a feasible OCV-resistance current root."""

from __future__ import annotations

import math
from collections.abc import Mapping

from data_generator.application.model.common import (
    apparent,
    clamp,
    finite_outputs,
    number,
    optional_number,
    positive,
)
from data_generator.application.model.weather import Weather
from data_generator.domain.model import GeneratorError


class BESS:
    """The internal resistance dissipates heat, not a second PCS efficiency loss."""

    def __init__(self, params: Mapping[str, object]) -> None:
        self.p = params
        positive(
            params,
            "rated_energy_wh",
            "pcs_rated_active_power_w",
            "pcs_rated_apparent_power_va",
            "charge_efficiency",
            "discharge_efficiency",
            "max_battery_current_a",
            "soc_control_gain_w",
        )
        self.soc_low = number(params, "soc_min", minimum=0)
        self.soc_high = number(params, "soc_max")
        initial = number(params, "initial_soc")
        if not 0 <= self.soc_low < initial < self.soc_high <= 1:
            raise GeneratorError("invalid BESS SOC interval")
        if number(params, "pcs_rated_apparent_power_va") < number(
            params, "pcs_rated_active_power_w"
        ):
            raise GeneratorError("BESS PCS apparent rating too small")
        for name in ("charge_efficiency", "discharge_efficiency"):
            if number(params, name) > 1:
                raise GeneratorError("BESS PCS efficiency exceeds one")
        self.resistance = number(params, "internal_resistance_ohm", minimum=0)
        strategy = params.get("control_strategy")
        if strategy not in {"soc_target", "power_demand"}:
            raise GeneratorError("unknown BESS control strategy")
        if type(params.get("reactive_support_enabled")) is not bool:
            raise GeneratorError("BESS reactive capability must be boolean")
        raw_curve = params.get("ocv_curve")
        if not isinstance(raw_curve, list) or len(raw_curve) < 2:
            raise GeneratorError("BESS requires an OCV curve")
        self.ocv_curve: list[tuple[float, float]] = []
        for raw in raw_curve:
            if not isinstance(raw, dict):
                raise GeneratorError("invalid BESS OCV curve entry")
            soc = number(raw, "soc", minimum=0)
            voltage = number(raw, "voltage_v")
            if soc > 1 or voltage <= 0 or (self.ocv_curve and soc <= self.ocv_curve[-1][0]):
                raise GeneratorError("invalid BESS OCV curve ordering")
            self.ocv_curve.append((soc, voltage))
        if self.ocv_curve[0][0] > self.soc_low or self.ocv_curve[-1][0] < self.soc_high:
            raise GeneratorError("OCV curve must cover the full allowed SOC interval")
        self.energy = initial * number(params, "rated_energy_wh") * 3600
        self.soh = optional_number(params, "initial_soh", 1.0)
        if not 0 < self.soh <= 1:
            raise GeneratorError("invalid initial SOH")
        self.temperature = number(params, "initial_battery_temperature_c")
        thermal = params.get("thermal_parameters")
        if not isinstance(thermal, dict):
            raise GeneratorError("BESS thermal_parameters required")
        self.heat_capacity = number(thermal, "thermal_capacity_j_per_k")
        self.cooling = number(thermal, "cooling_w_per_k", minimum=0)
        if self.heat_capacity <= 0:
            raise GeneratorError("BESS thermal capacity must be positive")

    def _ocv(self, soc: float) -> float:
        curve = self.ocv_curve
        for (x0, y0), (x1, y1) in zip(curve, curve[1:], strict=True):
            if soc <= x1:
                return y0 + (y1 - y0) * (soc - x0) / (x1 - x0)
        return curve[-1][1]

    def _candidate(self, ac_power: float, dt: float) -> tuple[float, float, float] | None:
        p = self.p
        eta_dis = number(p, "discharge_efficiency")
        eta_ch = number(p, "charge_efficiency")
        dc_power = ac_power / eta_dis if ac_power >= 0 else ac_power * eta_ch
        rated = number(p, "rated_energy_wh") * 3600
        ocv = self._ocv(self.energy / rated)
        discriminant = ocv**2 - 4 * self.resistance * dc_power
        if discriminant < 0:
            return None
        denom = ocv + math.sqrt(discriminant)
        if denom <= 0:
            return None
        current = 2 * dc_power / denom
        terminal_voltage = ocv - self.resistance * current
        next_energy = self.energy - dt * ocv * current
        if (
            terminal_voltage <= 0
            or abs(current) > number(p, "max_battery_current_a")
            or next_energy < self.soc_low * rated - 1e-7
            or next_energy > self.soc_high * rated + 1e-7
        ):
            return None
        return next_energy, current, terminal_voltage

    def _feasible(self, requested: float, dt: float) -> tuple[float, float, float, float]:
        choice = self._candidate(requested, dt)
        if choice is not None:
            return (requested, *choice)
        low, high = 0.0, 1.0
        for _ in range(48):
            mid = (low + high) / 2
            if self._candidate(requested * mid, dt) is None:
                high = mid
            else:
                low = mid
        feasible = self._candidate(requested * low, dt)
        if feasible is None:
            raise GeneratorError("BESS cannot find a feasible zero-power state")
        return requested * low, *feasible

    def advance(
        self, dt: float, weather: Weather, controls: Mapping[str, float], voltage: float
    ) -> dict[str, float | int]:
        p = self.p
        rated = number(p, "rated_energy_wh") * 3600
        soc = self.energy / rated
        deadband = optional_number(p, "soc_deadband", 0)
        if deadband < 0:
            raise GeneratorError("invalid BESS SOC deadband")
        target_soc = clamp(float(controls.get("soc_target", 50)) / 100, self.soc_low, self.soc_high)
        demand = float(controls.get("active_power_setpoint", 0))
        if p["control_strategy"] == "soc_target":
            requested = (soc - target_soc) * number(p, "soc_control_gain_w")
            if abs(soc - target_soc) <= deadband:
                requested = 0.0
        else:
            requested = demand
        power_cap = number(p, "pcs_rated_active_power_w")
        requested = clamp(requested, -power_cap, power_cap)
        if not bool(controls.get("_grid_connected", 1)):
            requested = 0.0
        power, next_energy, current, terminal_voltage = self._feasible(requested, dt)
        self.energy = clamp(next_energy, self.soc_low * rated, self.soc_high * rated)
        loss = current**2 * self.resistance
        self.temperature += (
            dt
            * (loss - self.cooling * (self.temperature - weather.ambient_temperature))
            / self.heat_capacity
        )
        aging = optional_number(p, "aging_coefficient_per_ampere_second", 0.0)
        if aging < 0:
            raise GeneratorError("invalid aging coefficient")
        self.soh = clamp(self.soh - aging * abs(current) * dt, 0, 1)
        s_cap = number(p, "pcs_rated_apparent_power_va")
        q_cap = math.sqrt(max(0, s_cap**2 - power**2))
        reactive = (
            clamp(float(controls.get("reactive_power_setpoint", 0)), -q_cap, q_cap)
            if p["reactive_support_enabled"] and bool(controls.get("_grid_connected", 1))
            else 0.0
        )
        _, pcs_current, pf = apparent(power, reactive, voltage)
        return finite_outputs(
            {
                "soc": 100 * self.energy / rated,
                "soh": 100 * self.soh,
                "stored_energy": self.energy / 3600,
                "available_charge_power": -self._feasible(-power_cap, dt)[0],
                "available_discharge_power": self._feasible(power_cap, dt)[0],
                "battery_voltage": terminal_voltage,
                "battery_current": current,
                "battery_loss": loss,
                "battery_temperature": self.temperature,
                "active_power": power,
                "reactive_power": reactive,
                "pcs_current": pcs_current,
                "sim_power_factor": pf,
                "pcs_status": int(bool(controls.get("_grid_connected", 1))),
                "bms_status": 1,
                "fault_status": 0,
                "control_result": 0,
                "soc_target": 100 * target_soc,
                "active_power_setpoint": demand,
                "reactive_power_setpoint": reactive,
            }
        )
