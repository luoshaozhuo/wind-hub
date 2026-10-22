"""2 MW 简化风机的动态遥测与控制读回。

功率单位 kW，扭矩 N·m，转速 rpm。非厂家标定模型。
"""
from __future__ import annotations

import math
import random

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import DeviceSnapshot, GridSample, Scalar, SimulationTime, WindSample
from seahorse.infrastructure.models.wind_turbine.history import TimeWindow
from seahorse.infrastructure.models.wind_turbine.contract import validate_turbine_points


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _delta(target: float, current: float) -> float:
    return (target - current + 180) % 360 - 180


class SimpleWindTurbine:
    def __init__(self, device_id: str, seed: int, rated_power_kw: float = 2000.0) -> None:
        if not math.isfinite(rated_power_kw) or rated_power_kw <= 0:
            raise ValueError("rated power must be positive and finite")
        self._id = device_id
        self._rng = random.Random(seed)
        self._rated = rated_power_kw
        self._yaw = 270.0
        self._pitch = 0.0
        self._rotor = 0.0
        self._power = 0.0
        self._reactive = 0.0
        self._energy = 0.0
        self._limit = rated_power_kw
        self._q_target = 0.0
        self._ramp = 200.0
        self._running = True
        self._fault = False
        self._mode = 0
        self._inertia_limit = 0.0
        self._reactive_status = 0
        self._controls: dict[str, Scalar] = {}
        self._history = {
            name: (TimeWindow(10), TimeWindow(60))
            for name in ("wind", "active", "reactive")
        }
        self._temperatures = {"emsEnvTemp": 20.0, "emsCabinTemp": 30.0, "emsOilTemp": 45.0}
        self._state = DeviceSnapshot(device_id, {})

    @property
    def device_id(self) -> str:
        return self._id

    def apply(self, command: EquipmentCommand) -> None:
        point, value = command.point_id, command.value
        if point in {"ctrlStart", "ctrlStop", "ctrlReset"}:
            if not isinstance(value, bool):
                raise ValueError("start/stop/reset require bool")
            if value:
                if point == "ctrlStop":
                    self._running = False
                elif point == "ctrlReset":
                    self._fault = False
                elif not self._fault:
                    self._running = True
        elif point in {"emsActCtrlMode", "emsReactPowerSetStatusWrite"}:
            if type(value) is not int or not 0 <= value <= 2:
                raise ValueError("invalid control mode")
            if point == "emsActCtrlMode":
                self._mode = value
            else:
                self._reactive_status = value
        elif point in {
            "ctrlActPow", "ctrlReActPow", "emsActPowerChangeRate", "emsActPowerSetInertia"
        }:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError("setpoint must be numeric")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("non-finite command")
            if point == "ctrlActPow":
                if not 0 <= number <= self._rated:
                    raise ValueError("active limit out of range")
                self._limit = number
            elif point == "ctrlReActPow":
                if abs(number) > 1.1 * self._rated:
                    raise ValueError("reactive setpoint out of range")
                self._q_target = number
            elif point == "emsActPowerChangeRate":
                if number <= 0:
                    raise ValueError("ramp rate must be positive")
                self._ramp = number
            else:
                if not 0 <= number <= self._rated:
                    raise ValueError("inertia value out of range")
                self._inertia_limit = number
        else:
            raise ValueError(f"unsupported control: {point}")
        self._controls[point] = value

    def step(self, time: SimulationTime, wind: WindSample, grid: GridSample) -> None:
        dt = time.dt_seconds
        old_power = self._power
        yaw_error = _delta(wind.direction_deg, self._yaw)
        self._yaw = (self._yaw + _clip(yaw_error, -0.5 * dt, 0.5 * dt)) % 360
        effective_wind = wind.speed_mps * max(0.0, math.cos(math.radians(yaw_error)))
        operational = self._running and not self._fault and 3 <= wind.speed_mps < 25
        target_rotor = 7.5 * effective_wind / 43.5 if operational else 0.0
        self._rotor += (1 - math.exp(-dt / 8)) * (target_rotor - self._rotor)
        tip_ratio = self._rotor * 43.5 / max(effective_wind, 0.01)
        cp = 0.45 * math.exp(-0.03 * (tip_ratio - 7.5) ** 2 - 0.04 * self._pitch)
        aero_kw = 0.5 * 1.225 * math.pi * 43.5**2 * effective_wind**3 * cp / 1000
        available = min(self._rated, 0.93 * aero_kw) if operational else 0.0
        target = min(available, self._limit)
        slope = (1 - math.exp(-dt / 5)) * (target - old_power)
        self._power = max(0.0, old_power + _clip(slope, -self._ramp * dt, self._ramp * dt))
        if operational:
            self._power = min(self._power, available)
        pitch_target = 20.0 if operational and self._limit < available else 0.0
        self._pitch += _clip(pitch_target - self._pitch, -5 * dt, 5 * dt)
        q_max = math.sqrt(max(0.0, (1.1 * self._rated) ** 2 - self._power**2))
        q_target = _clip(self._q_target, -q_max, q_max)
        self._reactive += (1 - math.exp(-dt / 2)) * (q_target - self._reactive)
        self._reactive = _clip(self._reactive, -q_max, q_max)
        self._energy += (old_power + self._power) * dt / 7200
        generator_omega = self._rotor * 90
        generator_rpm = generator_omega * 60 / (2 * math.pi)
        torque = 1000 * self._power / max(generator_omega, 1.0)

        for name, value in (
            ("wind", wind.speed_mps), ("active", self._power),
            ("reactive", self._reactive),
        ):
            for window in self._history[name]:
                window.add(time.seconds, value)
        for point, mean, std in (
            ("emsEnvTemp", 20.0, 1.0),
            ("emsCabinTemp", 30.0, 1.5),
            ("emsOilTemp", 45.0, 2.0),
        ):
            a = math.exp(-dt / 30)
            self._temperatures[point] = (
                mean + a * (self._temperatures[point] - mean)
                + std * math.sqrt(1 - a * a) * self._rng.gauss(0, 1)
            )
        wa, wb = self._history["wind"]
        pa, pb = self._history["active"]
        qa, qb = self._history["reactive"]
        current = (
            1000 * math.hypot(self._power, self._reactive)
            / (math.sqrt(3) * 690)
        )
        self._state = DeviceSnapshot(
            self._id,
            {
                "emsWorkMode": int(operational),
                "emsWinSpd": wind.speed_mps,
                "emsWinSpdAvg10Sec": wa.average(),
                "emsWinSpdAvg60Sec": wb.average(),
                "emsBladeAngle1": self._pitch,
                "emsBladeAngle2": self._pitch,
                "emsBladeAngle3": self._pitch,
                "emsMinBladeAngle": self._pitch,
                "emsGenRotateSpd": generator_rpm,
                "emsGenTorque": torque,
                "emsPowAct": self._power,
                "emsPowActCvt": self._power / 0.98,
                "emsPowActAvg10Sec": pa.average(),
                "emsPowActAvg60Sec": pb.average(),
                "emsPowReact": self._reactive,
                "emsPowReactCvt": self._reactive / 0.98,
                "emsPowReact10": qa.average(),
                "emsPowReact60": qb.average(),
                "emsGridPhaseCurrentA": current,
                "emsConvertVol": 690.0,
                "emsTotalEnergy": self._energy,
                **self._temperatures,
                "emsFaultState": self._fault,
                "emsLocalOnOff": self._running,
                "emsAutoLimitState": self._limit < self._rated,
                "emsLocalLimitState": False,
                "emsRemoteLimitState": self._limit < self._rated,
                "emsLimitStop": False,
                "emsLocalActPowerEnableState": True,
                "emsReactCanCtrl": not self._fault,
                "emsAutoLimitSetFeedback": self._limit < self._rated,
                "emsActPowerMaxSetFeedback": self._limit,
                "emsReactPowerMaxSetFeedback": self._q_target,
                "emsReactPowerSet": self._q_target,
                "emsActPowerSet": self._limit,
                "emsActPowerRead": self._limit,
                "emsReactPowerSetStatus": self._reactive_status,
                "inertiaLimitValue": self._inertia_limit,
                "emsActPowerSetInertia": self._inertia_limit,
                "emsActPowerChangeRate": self._ramp,
                "emsActCtrlMode": self._mode,
                "ctrlStart": bool(self._controls.get("ctrlStart", False)),
                "ctrlStop": bool(self._controls.get("ctrlStop", False)),
                "ctrlReset": bool(self._controls.get("ctrlReset", False)),
            },
        )

    def snapshot(self) -> DeviceSnapshot:
        if self._state.points:
            validate_turbine_points(set(self._state.points))
        return self._state
