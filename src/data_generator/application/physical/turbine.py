"""Rigid-drive wind turbine with one electromagnetic torque actuator."""

from __future__ import annotations

import math
from collections.abc import Mapping

from data_generator.application.physical.common import (
    apparent,
    clamp,
    finite_outputs,
    number,
    optional_number,
    positive,
    response,
)
from data_generator.application.physical.weather import Weather
from data_generator.domain.model import GeneratorError


class Turbine:
    """Three pitch states; aerodynamic energy drives the shared rotor inertia."""

    def __init__(self, params: Mapping[str, object]) -> None:
        self.p = params
        positive(params, "rated_power_w", "rated_apparent_power_va", "rated_wind_speed_m_s",
                 "rated_rotor_speed_rad_s", "max_rotor_speed_rad_s", "cut_out_speed_m_s",
                 "rotor_radius_m", "rotor_inertia_kg_m2", "generator_inertia_kg_m2",
                 "gear_ratio", "lambda_opt", "cp_opt", "generator_efficiency",
                 "converter_efficiency", "max_generator_torque_nm", "torque_tau_s",
                 "pitch_tau_s", "pitch_rate_rad_s", "pitch_speed_gain_s",
                 "yaw_tau_s", "yaw_rate_rad_s")
        if not (0 <= number(params, "cut_in_speed_m_s") < number(params, "rated_wind_speed_m_s") < number(params, "cut_out_speed_m_s")):
            raise GeneratorError("invalid turbine wind speed protection thresholds")
        if number(params, "rated_apparent_power_va") < number(params, "rated_power_w"):
            raise GeneratorError("turbine apparent capacity below active rating")
        if number(params, "rated_rotor_speed_rad_s") >= number(params, "max_rotor_speed_rad_s"):
            raise GeneratorError("turbine rated rotor speed must be less than maximum")
        number(params, "pitch_power_gain_rad_w", minimum=0)
        number(params, "shaft_damping_nm_s_rad", minimum=0)
        self.min_pitch = number(params, "pitch_min_rad", minimum=0)
        self.feather = number(params, "pitch_feather_rad")
        if self.feather <= self.min_pitch:
            raise GeneratorError("invalid turbine feathering range")
        for key in ("cp_opt", "generator_efficiency", "converter_efficiency"):
            if number(params, key) > (16 / 27 if key == "cp_opt" else 1):
                raise GeneratorError(f"invalid turbine efficiency: {key}")
        curve = params.get("cp_curve")
        if not isinstance(curve, list) or len(curve) < 2:
            raise GeneratorError("turbine requires a Cp curve with at least two rows")
        self.cp_curve: list[tuple[float, float]] = []
        for entry in curve:
            if not isinstance(entry, dict):
                raise GeneratorError("invalid turbine Cp curve row")
            lam = number(entry, "lambda", minimum=0)
            cp = number(entry, "cp", minimum=0)
            if cp > 16 / 27 or (self.cp_curve and lam <= self.cp_curve[-1][0]):
                raise GeneratorError("nonmonotonic or nonphysical turbine Cp curve")
            self.cp_curve.append((lam, cp))
        rated = number(params, "rated_rotor_speed_rad_s")
        radius = number(params, "rotor_radius_m")
        rho = optional_number(params, "reference_air_density_kg_m3", 1.225)
        if rho <= 0:
            raise GeneratorError("reference air density must be positive")
        lam = rated * radius / number(params, "rated_wind_speed_m_s")
        p_available = 0.5 * rho * math.pi * radius**2 * self._cp(lam, self.min_pitch) * number(params, "rated_wind_speed_m_s")**3
        if p_available * number(params, "generator_efficiency") * number(params, "converter_efficiency") < 0.95 * number(params, "rated_power_w"):
            raise GeneratorError("turbine rated reference point cannot produce rated power")
        self.omega = optional_number(params, "initial_rotor_speed_rad_s", rated)
        self.yaw = optional_number(params, "initial_yaw_rad", 0.0)
        self.pitch = [optional_number(params, "initial_pitch_rad", self.min_pitch)] * 3
        self.torque = optional_number(params, "initial_generator_torque_nm", 0.0)
        self.temperature = optional_number(params, "initial_generator_temperature_c", 25.0)
        self.reactive = 0.0
        self.last_power = 0.0
        self.fault = 0

    def _cp(self, lam: float, pitch: float) -> float:
        points = self.cp_curve
        cp = 0.0
        if points[0][0] <= lam <= points[-1][0]:
            for (x0, y0), (x1, y1) in zip(points, points[1:], strict=True):
                if lam <= x1:
                    cp = y0 + (y1 - y0) * (lam - x0) / (x1 - x0)
                    break
        pitch_factor = clamp(1 - (pitch - self.min_pitch) / (self.feather - self.min_pitch), 0, 1)
        return clamp(cp * pitch_factor, 0, 16 / 27)

    def advance(
        self, dt: float, weather: Weather, controls: Mapping[str, float], voltage: float
    ) -> dict[str, float | int]:
        p = self.p
        n = number(p, "gear_ratio")
        eta = number(p, "generator_efficiency") * number(p, "converter_efficiency")
        rated = number(p, "rated_power_w")
        rotor_max = number(p, "max_rotor_speed_rad_s")
        self.yaw += clamp(
            (weather.wind_direction - self.yaw + math.pi) % (2 * math.pi) - math.pi,
            -number(p, "yaw_rate_rad_s") * dt,
            number(p, "yaw_rate_rad_s") * dt,
        )
        yaw_err = (weather.wind_direction - self.yaw + math.pi) % (2 * math.pi) - math.pi
        wind = weather.wind_speed * max(0, math.cos(yaw_err))
        radius = number(p, "rotor_radius_m")
        lam = self.omega * radius / max(wind, 0.01)
        cp = self._cp(lam, sum(self.pitch) / 3)
        pa = 0.5 * weather.air_density * math.pi * radius**2 * cp * wind**3
        wind_ok = (
            number(p, "cut_in_speed_m_s") <= weather.wind_speed
            < number(p, "cut_out_speed_m_s")
        )
        # A cut-out or overspeed trip is latched for the Generation's lifetime.
        if weather.wind_speed >= number(p, "cut_out_speed_m_s") or self.omega > rotor_max:
            self.fault = 1
        active = wind_ok and bool(controls.get("_grid_connected", 1)) and not self.fault
        available = min(rated, pa * eta) if active and not self.fault else 0.0
        mode_value = controls.get("control_mode", 0)
        if mode_value not in (0, 1, 2):
            raise GeneratorError("unknown turbine control mode")
        mode = int(mode_value)
        demand = clamp(float(controls.get("active_power_setpoint", rated)), 0, rated)
        limit = clamp(float(controls.get("power_limit_setpoint", rated)), 0, rated)
        requested = available if mode == 0 else min(available, demand if mode == 1 else limit)
        if mode == 0:
            k_opt = (weather.air_density * math.pi * radius**5 * number(p, "cp_opt")
                     / (2 * number(p, "lambda_opt")**3))
            torque_target = k_opt * self.omega**2 / n
        else:
            torque_target = requested / max(eta * n * self.omega, 0.01)
        torque_cap = min(number(p, "max_generator_torque_nm"), rated / max(eta * n * self.omega, 0.01))
        if not active or self.fault:
            torque_target = 0.0
        self.torque = clamp(
            response(self.torque, clamp(torque_target, 0, torque_cap), dt, number(p, "torque_tau_s")),
            0, torque_cap,
        )
        pitch_target = clamp(
            self.min_pitch + number(p, "pitch_speed_gain_s")
            * max(0, self.omega - number(p, "rated_rotor_speed_rad_s"))
            + number(p, "pitch_power_gain_rad_w", minimum=0)
            * max(0, eta * pa - requested),
            self.min_pitch, self.feather,
        )
        if self.fault or not active:
            pitch_target = self.feather
        rates = []
        for i, beta in enumerate(self.pitch):
            rate = clamp(
                (pitch_target - beta) / number(p, "pitch_tau_s"),
                -number(p, "pitch_rate_rad_s"), number(p, "pitch_rate_rad_s"),
            )
            self.pitch[i] = clamp(beta + dt * rate, self.min_pitch, self.feather)
            rates.append(rate)
        ta = pa / self.omega if self.omega > 0.01 and active else 0.0
        inertia = number(p, "rotor_inertia_kg_m2") + n**2 * number(p, "generator_inertia_kg_m2")
        damping = number(p, "shaft_damping_nm_s_rad", minimum=0)
        old_omega = self.omega
        # Enforce the electrical rating on the torque actuator, not on output watts.
        for _ in range(6):
            predicted = old_omega + dt * (
                ta - n * self.torque - damping * old_omega
            ) / inertia
            if predicted < 0:
                raise GeneratorError("turbine integration would reverse rotor direction")
            if not active or eta * self.torque * n * predicted <= rated:
                break
            self.torque = min(self.torque, rated / (eta * n * max(predicted, 0.01)))
        predicted = old_omega + dt * (
            ta - n * self.torque - damping * old_omega
        ) / inertia
        if predicted > rotor_max:
            raise GeneratorError("turbine overspeed protection: no braking capacity configured")
        if predicted < 0:
            raise GeneratorError("turbine integration would reverse rotor direction")
        self.omega = predicted
        # Electrical power always follows the sole executed torque and rotor speed.
        power = eta * self.torque * n * self.omega if active and not self.fault else 0.0
        if power > rated * (1 + 1e-9):
            raise GeneratorError("turbine torque actuator violated electrical power rating")
        self.last_power = power
        q_bound = math.sqrt(max(0, number(p, "rated_apparent_power_va")**2 - power**2))
        q_target = clamp(float(controls.get("reactive_power_setpoint", 0)), -q_bound, q_bound)
        self.reactive = clamp(response(self.reactive, q_target, dt, optional_number(p, "reactive_tau_s", 0.5)), -q_bound, q_bound)
        loss = (1 - eta) * self.torque * n * self.omega
        heat_capacity = optional_number(p, "thermal_capacity_j_per_k", 1000000.0)
        cooling = optional_number(p, "cooling_w_per_k", 1000.0)
        if heat_capacity <= 0 or cooling < 0:
            raise GeneratorError("invalid turbine thermal parameters")
        self.temperature += dt * (loss - cooling * (self.temperature - weather.ambient_temperature)) / heat_capacity
        _, current, pf = apparent(power, self.reactive, voltage)
        return finite_outputs({
            "wind_speed": weather.wind_speed,
            "effective_wind_speed": wind,
            "yaw_angle": math.degrees(self.yaw),
            "yaw_error": math.degrees(yaw_err),
            "tip_speed_ratio": lam,
            "power_coefficient": cp,
            "pitch_angle_1": math.degrees(self.pitch[0]),
            "pitch_angle_2": math.degrees(self.pitch[1]),
            "pitch_angle_3": math.degrees(self.pitch[2]),
            "pitch_rate_1": math.degrees(rates[0]),
            "pitch_rate_2": math.degrees(rates[1]),
            "pitch_rate_3": math.degrees(rates[2]),
            "sim_rotor_speed": self.omega,
            "sim_generator_speed": n * self.omega,
            "shaft_torque": n * self.torque,
            "aerodynamic_power": pa,
            "aerodynamic_torque": ta,
            "available_power": available,
            "sim_generator_torque": self.torque,
            "active_power": power,
            "reactive_power": self.reactive,
            "converter_current": current,
            "converter_losses": loss,
            "sim_power_factor": pf,
            "generator_temperature": self.temperature,
            "running_status": int(active and not self.fault),
            "fault_status": self.fault,
            "control_result": 0,
            "control_mode": mode,
            "active_power_setpoint": demand,
            "power_limit_setpoint": limit,
            "reactive_power_setpoint": q_target,
        })
