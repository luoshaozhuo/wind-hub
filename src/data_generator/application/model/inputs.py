"""Writable simulation inputs and weather controls, independent of Modbus encoding."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from data_generator.domain.model import GeneratorError, Scalar, validate_scalar


@dataclass(frozen=True, slots=True)
class InputSpec:
    data_type: str
    unit: str
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""


_INPUTS: dict[str, dict[str, InputSpec]] = {
    "wtg": {
        "control_mode": InputSpec("int16", "none", 0, 2, "0 optimal, 1 constant, 2 curtailed"),
        "active_power_setpoint": InputSpec("float32", "watt", 0),
        "power_limit_setpoint": InputSpec("float32", "watt", 0),
        "reactive_power_setpoint": InputSpec("float32", "var"),
    },
    "pv": {
        "active_power_setpoint": InputSpec("float32", "watt", 0),
        "reactive_power_setpoint": InputSpec("float32", "var"),
    },
    "bess": {
        "soc_target": InputSpec("float32", "percent", 0, 100),
        "active_power_setpoint": InputSpec("float32", "watt"),
        "reactive_power_setpoint": InputSpec("float32", "var"),
    },
    "grid": {
        "grid_frequency_input": InputSpec("float32", "hertz", 0),
        "grid_voltage_source": InputSpec("float32", "volt", 0),
        "grid_connected": InputSpec("uint16", "none", 0, 1),
    },
    "ppc": {
        "agc_enable": InputSpec("uint16", "none", 0, 1),
        "agc_power_setpoint": InputSpec("float32", "watt"),
        "avc_enable": InputSpec("uint16", "none", 0, 1),
        "avc_voltage_setpoint": InputSpec("float32", "volt", 0),
        "avc_reactive_setpoint": InputSpec("float32", "var"),
        "ffr_enable": InputSpec("uint16", "none", 0, 1),
    },
}

WEATHER_INPUTS: dict[str, InputSpec] = {
    "wind_speed_mean": InputSpec("float32", "meter_per_second", 0),
    "wind_direction": InputSpec("float32", "degree", 0, 360),
}


def writable_inputs(kind: str) -> Mapping[str, InputSpec]:
    return _INPUTS.get(kind, {})


def validate_input(value: Scalar, spec: InputSpec) -> float | int:
    checked = validate_scalar(value, spec.data_type, spec.minimum, spec.maximum)
    if type(checked) not in (int, float):
        raise GeneratorError("model input must be a numeric scalar")
    return checked


def validate_weather_input(value: Scalar, spec: InputSpec) -> float:
    checked = validate_scalar(value, spec.data_type, spec.minimum, spec.maximum)
    if type(checked) not in (int, float):
        raise GeneratorError("weather input must be numeric")
    return float(checked)
