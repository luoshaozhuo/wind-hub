"""Plant-level AGC/AVC/FFR using one-step-delayed device feedback."""

from __future__ import annotations

import math
from collections.abc import Mapping

from data_generator.application.physical.common import clamp, finite_outputs, number
from data_generator.domain.model import GeneratorError


class PPC:
    def __init__(self, params: Mapping[str, object]) -> None:
        self.p = params
        ids = params.get("participating_device_ids")
        if not isinstance(ids, list) or not ids:
            raise GeneratorError("invalid PPC participating devices")
        if not all(isinstance(item, str) and item for item in ids):
            raise GeneratorError("invalid PPC device ID")
        if len(ids) != len(set(ids)):
            raise GeneratorError("duplicate PPC participating device")
        weights = params.get("allocation_weights")
        if not isinstance(weights, dict) or set(weights) != set(ids):
            raise GeneratorError("invalid PPC allocation weights")
        self.devices = tuple(sorted(ids))
        self.weights = {key: number(weights, key, minimum=0) for key in self.devices}
        if not any(self.weights.values()):
            raise GeneratorError("PPC requires positive allocation weights")
        if params.get("avc_mode") not in {"reactive_power", "voltage"}:
            raise GeneratorError("unknown PPC AVC mode")
        number(params, "agc_ramp_w_s", minimum=0)
        number(params, "ffr_deadband_hz", minimum=0)
        number(params, "ffr_droop_w_hz", minimum=0)
        self.last_dispatch = {device_id: 0.0 for device_id in self.devices}

    def _allocate(
        self, total: float, lower: Mapping[str, float], upper: Mapping[str, float]
    ) -> dict[str, float]:
        minimum = sum(lower.values())
        maximum = sum(
            upper[key] if self.weights[key] > 0 else lower[key]
            for key in self.devices
        )
        target = clamp(total, minimum, maximum)
        lo, hi = 0.0, max((upper[key] - lower[key]) / self.weights[key]
                          for key in self.devices if self.weights[key] > 0)
        for _ in range(52):
            mid = (lo + hi) / 2
            distributed = sum(clamp(lower[k] + mid * self.weights[k], lower[k], upper[k])
                              for k in self.devices)
            if distributed < target:
                lo = mid
            else:
                hi = mid
        return {key: clamp(lower[key] + hi * self.weights[key], lower[key], upper[key])
                for key in self.devices}

    def advance(
        self,
        dt: float,
        previous: Mapping[str, Mapping[str, float | int]],
        grid: Mapping[str, float | int],
        controls: Mapping[str, float],
        limits: Mapping[str, tuple[float, float]],
    ) -> tuple[dict[str, dict[str, float]], dict[str, float | int]]:
        if set(self.devices) - set(previous):
            raise GeneratorError("PPC previous feedback missing participant")
        power = sum(float(previous[k].get("active_power", 0)) for k in self.devices)
        reactive = sum(float(previous[k].get("reactive_power", 0)) for k in self.devices)
        available = sum(max(0, upper) for _, upper in limits.values())
        agc_on = int(controls.get("agc_enable", 0))
        avc_on = int(controls.get("avc_enable", 0))
        ffr_on = int(controls.get("ffr_enable", 0))
        if any(value not in (0, 1) for value in (agc_on, avc_on, ffr_on)):
            raise GeneratorError("PPC enable must be zero or one")
        frequency = float(grid.get("grid_frequency", 50.0))
        nominal = float(grid.get("frequency_nominal", 50.0))
        deviation = frequency - nominal
        deadband = number(self.p, "ffr_deadband_hz")
        droop = number(self.p, "ffr_droop_w_hz")
        ffr = (-droop * (deviation - math.copysign(deadband, deviation))
               if ffr_on and abs(deviation) > deadband else 0.0)
        command = float(controls.get("agc_power_setpoint", power)) if agc_on else power
        command += ffr
        lower = {}
        upper = {}
        ramp = number(self.p, "agc_ramp_w_s") * dt
        for device in self.devices:
            low, high = limits[device]
            prior = float(previous[device].get("active_power", 0))
            lower[device] = max(low, prior - ramp)
            upper[device] = min(high, prior + ramp)
            if lower[device] > upper[device]:
                raise GeneratorError("PPC device ramp and capacity limits inconsistent")
        allocation = self._allocate(command, lower, upper)
        self.last_dispatch = allocation
        voltage = float(grid.get("grid_voltage", 0))
        q_req = 0.0
        if avc_on:
            q_req = (float(controls.get("avc_reactive_setpoint", 0))
                     if self.p["avc_mode"] == "reactive_power" else
                     optional_voltage_control(self.p, float(controls.get("avc_voltage_setpoint", voltage)), voltage))
        q_caps = {k: math.sqrt(max(0, float(previous[k].get("rated_apparent_power", 0))**2
                                     - float(previous[k].get("active_power", 0))**2))
                  for k in self.devices}
        q_sum = sum(q_caps.values())
        reactive_alloc = {k: (clamp(q_req, -q_sum, q_sum) * q_caps[k] / q_sum if q_sum else 0.0)
                          for k in self.devices}
        dispatch = {k: {"active_power_setpoint": allocation[k],
                         "reactive_power_setpoint": reactive_alloc[k]} for k in self.devices}
        return dispatch, finite_outputs({
            "active_power_feedback": power,
            "agc_allocated_power": sum(allocation.values()),
            "agc_tracking_error": command - power,
            "grid_voltage_feedback": voltage,
            "avc_allocated_reactive_power": sum(reactive_alloc.values()),
            "avc_tracking_error": q_req - reactive,
            "grid_frequency_feedback": frequency,
            "ffr_power_command": ffr,
            "ffr_active": int(ffr != 0),
            "frequency_deviation": deviation,
            "plant_active_power": power,
            "plant_reactive_power": reactive,
            "plant_available_power": available,
            "unserved_power_command": command - sum(allocation.values()),
            "agc_enable": agc_on,
            "agc_power_setpoint": float(controls.get("agc_power_setpoint", power)),
            "avc_enable": avc_on,
            "avc_voltage_setpoint": float(controls.get("avc_voltage_setpoint", voltage)),
            "avc_reactive_setpoint": float(controls.get("avc_reactive_setpoint", 0)),
            "ffr_enable": ffr_on,
        })


def optional_voltage_control(params: Mapping[str, object], target: float, actual: float) -> float:
    gain = float(params.get("avc_voltage_gain_var_v", 0.0))
    if not math.isfinite(gain) or gain < 0:
        raise GeneratorError("invalid AVC voltage control gain")
    return gain * (target - actual)
