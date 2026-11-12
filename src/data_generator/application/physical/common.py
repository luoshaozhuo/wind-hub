"""Numerical invariants shared by the fixed physical models."""

from __future__ import annotations

import math
from collections.abc import Mapping

from data_generator.domain.model import GeneratorError


def number(params: Mapping[str, object], key: str, *, minimum: float | None = None) -> float:
    value = params.get(key)
    if type(value) not in (float, int) or not math.isfinite(float(value)):
        raise GeneratorError(f"invalid or missing physical parameter: {key}")
    result = float(value)
    if minimum is not None and result < minimum:
        raise GeneratorError(f"physical parameter below minimum: {key}")
    return result


def optional_number(params: Mapping[str, object], key: str, default: float) -> float:
    value = params.get(key, default)
    if type(value) not in (float, int) or not math.isfinite(float(value)):
        raise GeneratorError(f"invalid physical parameter: {key}")
    return float(value)


def clamp(value: float, low: float, high: float) -> float:
    if not (math.isfinite(value) and math.isfinite(low) and math.isfinite(high)) or low > high:
        raise GeneratorError("invalid physical model clamp")
    return min(high, max(low, value))


def response(current: float, target: float, dt: float, tau: float) -> float:
    if dt <= 0 or tau <= 0:
        raise GeneratorError("physical time step and time constant must be positive")
    return target + (current - target) * math.exp(-dt / tau)


def apparent(p: float, q: float, voltage: float) -> tuple[float, float, float]:
    if voltage <= 0:
        raise GeneratorError("grid voltage must be positive")
    s = math.hypot(p, q)
    return s, s / (math.sqrt(3.0) * voltage), p / s if s else 1.0


def finite_outputs(output: Mapping[str, float | int]) -> dict[str, float | int]:
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in output.values()):
        raise GeneratorError("physical model produced non-finite or nonnumeric output")
    return dict(output)


def as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
        raise GeneratorError(f"invalid physical model mapping: {label}")
    return value


def positive(params: Mapping[str, object], *names: str) -> None:
    for name in names:
        if number(params, name) <= 0:
            raise GeneratorError(f"physical parameter must be positive: {name}")
