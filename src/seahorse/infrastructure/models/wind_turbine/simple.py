"""2 MW 级简化机组：气动、变桨/偏航、有功/无功和基本控制反馈。

仅用于合理动态数据生成，不用于实际惯量响应或控制系统精度验证。
"""
from __future__ import annotations

import math
import random

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import DeviceSnapshot, GridSample, Scalar, SimulationTime, WindSample


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _angle_delta(target: float, current: float) -> float:
    return (target - current + 180) % 360 - 180


class SimpleWindTurbine:
    def __init__(self, device_id: str, seed: int, rated_power_kw: float = 2000.0) -> None:
        self._id = device_id
        self._rng = random.Random(seed)
        self._rated = rated_power_kw
        self._yaw_deg = 270.0
        self._pitch_deg = 0.0
        self._rotor_rad_s = 0.0
        self._power_kw = 0.0
        self._reactive_kvar = 0.0
        self._energy_kwh = 0.0
        self._limit_kw = rated_power_kw
        self._reactive_target = 0.0
        self._ramp_kw_s = 200.0
        self._running = True
        self._fault = False
        self._controls: dict[str, Scalar] = {}
        self._state = DeviceSnapshot(device_id, {})

    @property
    def device_id(self) -> str:
        return self._id

    def apply(self, command: EquipmentCommand) -> None:
        point = command.point_id
        value = command.value
        if point in {"ctrlStart", "ctrlStop", "ctrlReset"}:
            if not isinstance(value, bool):
                raise ValueError(f"{point} requires a boolean")
            if value:
                if point == "ctrlStop":
                    self._running = False
                elif point == "ctrlReset":
                    self._fault = False
                elif not self._fault:
                    self._running = True
        elif point in {"ctrlActPow", "ctrlReActPow", "emsActPowerChangeRate"}:
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError("control must be finite numeric")
            number = float(value)
            if point == "ctrlActPow":
                self._limit_kw = _clip(number, 0.0, self._rated)
            elif point == "ctrlReActPow":
                self._reactive_target = number
            elif number > 0:
                self._ramp_kw_s = number
            else:
                raise ValueError("ramp must be positive")
        else:
            raise ValueError(f"unsupported control: {point}")
        self._controls[point] = value

    def step(self, time: SimulationTime, wind: WindSample, grid: GridSample) -> None:
        dt = time.dt_seconds
        old_power = self._power_kw
        yaw_error = _angle_delta(wind.direction_deg, self._yaw_deg)
        self._yaw_deg = (
            self._yaw_deg + _clip(yaw_error, -0.5 * dt, 0.5 * dt)
        ) % 360
        effective_wind = wind.speed_mps * max(0.0, math.cos(math.radians(yaw_error)))
        operational = self._running and not self._fault and 3 <= wind.speed_mps < 25
        target_rotor = 7.5 * effective_wind / 43.5 if operational else 0.0
        speed_alpha = 1 - math.exp(-dt / 8)
        self._rotor_rad_s += speed_alpha * (target_rotor - self._rotor_rad_s)
        tip_ratio = self._rotor_rad_s * 43.5 / max(effective_wind, 0.01)
        cp = 0.45 * math.exp(-0.03 * (tip_ratio - 7.5) ** 2)
        cp *= math.exp(-0.04 * self._pitch_deg)
        aerodynamic_kw = (
            0.5 * 1.225 * math.pi * 43.5**2 * effective_wind**3 * cp / 1000
        )
        available = min(self._rated, 0.93 * aerodynamic_kw) if operational else 0.0
        target_power = min(available, self._limit_kw)
        power_alpha = 1 - math.exp(-dt / 5)
        change = _clip(
            power_alpha * (target_power - old_power),
            -self._ramp_kw_s * dt,
            self._ramp_kw_s * dt,
        )
        self._power_kw = _clip(old_power + change, 0, available if operational else old_power)
        pitch_target = 0.0 if target_power >= available else 20.0
        self._pitch_deg += _clip(pitch_target - self._pitch_deg, -5 * dt, 5 * dt)
        q_max = math.sqrt(max(0.0, (1.1 * self._rated) ** 2 - self._power_kw**2))
        reactive_target = _clip(self._reactive_target, -q_max, q_max)
        self._reactive_kvar += (1 - math.exp(-dt / 2)) * (
            reactive_target - self._reactive_kvar
        )
        self._energy_kwh += (old_power + self._power_kw) * dt / 7200
        generator_rpm = self._rotor_rad_s * 90 * 60 / (2 * math.pi)
        generator_torque = (
            1000 * self._power_kw / max(self._rotor_rad_s * 90, 1)
        )
        self._state = DeviceSnapshot(
            self._id,
            {
                "emsWinSpd": wind.speed_mps,
                "emsWorkMode": 1 if operational else 0,
                "emsFaultState": self._fault,
                "emsPowAct": self._power_kw,
                "emsPowReact": self._reactive_kvar,
                "emsTotalEnergy": self._energy_kwh,
                "emsGenRotateSpd": generator_rpm,
                "emsGenTorque": generator_torque,
                "emsBladeAngle1": self._pitch_deg,
                "emsBladeAngle2": self._pitch_deg,
                "emsBladeAngle3": self._pitch_deg,
                "emsMinBladeAngle": self._pitch_deg,
                "emsAutoLimitState": self._limit_kw < self._rated,
                "emsActPowerSet": self._limit_kw,
                "emsReactPowerSet": self._reactive_target,
                "emsEnvTemp": 20.0 + self._rng.gauss(0, 0.1),
                "emsCabinTemp": 30.0 + self._rng.gauss(0, 0.1),
                "emsOilTemp": 45.0 + self._rng.gauss(0, 0.1),
            },
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._state
