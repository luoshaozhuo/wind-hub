"""Deterministic random and stateless interpolation model evaluation."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from typing import Any

from data_generator.domain.model import GeneratorError, Scalar, validate_scalar


def random_value(
    seed: int,
    device_id: str,
    point_id: str,
    step: int,
    params: Mapping[str, object],
) -> Scalar:
    """Independent point/step PRNG streams are stable under device reordering."""
    if step < 1:
        raise GeneratorError("step must be positive")
    key = f"{seed}:{device_id}:{point_id}:{step}".encode("utf-8")
    point_seed = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
    rng = random.Random(point_seed)
    kind = str(params.get("data_type", "float64"))
    lower = params.get("minimum", 0)
    upper = params.get("maximum", 1)
    if (
        not isinstance(lower, int | float) or isinstance(lower, bool)
        or not isinstance(upper, int | float) or isinstance(upper, bool)
    ):
        raise GeneratorError("random bounds must be numeric")
    if not math.isfinite(float(lower)) or not math.isfinite(float(upper)):
        raise GeneratorError("random bounds must be finite")
    if lower > upper:
        raise GeneratorError("random minimum exceeds maximum")
    if params.get("distribution", "uniform") != "uniform":
        raise GeneratorError("unsupported random distribution")
    if kind == "bool":
        value: Scalar = rng.random() >= 0.5
    elif kind.startswith(("int", "uint")):
        if type(lower) is not int or type(upper) is not int:
            raise GeneratorError("integer bounds must be exact integers")
        value = rng.randint(lower, upper)
    elif kind in ("float32", "float64"):
        value = float(lower) + (float(upper) - float(lower)) * rng.random()
    else:
        raise GeneratorError(f"random generation does not support {kind}")
    return validate_scalar(value, kind)


def interpolation_table(params: Mapping[str, object]) -> tuple[tuple[float, float], ...]:
    """Validate full static model input table, even before model execution."""
    if params.get("function") != "linear_table":
        raise GeneratorError("unsupported static model function")
    source = params.get("input_point")
    if not isinstance(source, str) or not source:
        raise GeneratorError("static model requires input_point")
    values = params.get("values")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise GeneratorError("invalid interpolation table")
    if len(values) < 2:
        raise GeneratorError("interpolation needs two or more rows")
    table: list[tuple[float, float]] = []
    for entry in values:
        if not isinstance(entry, Sequence) or isinstance(entry, (str, bytes)):
            raise GeneratorError("invalid interpolation row")
        if len(entry) != 2 or any(type(value) not in (int, float) for value in entry):
            raise GeneratorError("interpolation row must contain two numeric values")
        x, y = float(entry[0]), float(entry[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise GeneratorError("interpolation values must be finite")
        if table and x <= table[-1][0]:
            raise GeneratorError("interpolation abscissa must strictly increase")
        table.append((x, y))
    if params.get("out_of_range", "reject") not in ("reject", "clamp"):
        raise GeneratorError("invalid interpolation boundary policy")
    return tuple(table)


def static_value(params: Mapping[str, object], inputs: Mapping[str, Scalar]) -> Scalar:
    """Calculate a table-based physical relation without state integration."""
    table = interpolation_table(params)
    raw = inputs.get(str(params["input_point"]))
    if (
        not isinstance(raw, int | float) or isinstance(raw, bool)
        or not math.isfinite(float(raw))
    ):
        raise GeneratorError("static model input missing or not finite")
    x = float(raw)
    if x < table[0][0] or x > table[-1][0]:
        if params.get("out_of_range", "reject") == "reject":
            raise GeneratorError("static input outside defined range")
        x = min(max(x, table[0][0]), table[-1][0])
    y = table[-1][1]
    for (left, low), (right, high) in zip(table, table[1:]):
        if x <= right:
            y = low + (high - low) * (x - left) / (right - left)
            break
    kind = str(params.get("data_type", "float64"))
    if kind.startswith(("int", "uint")):
        if not y.is_integer():
            raise GeneratorError("fractional static result cannot be encoded as integer")
        return validate_scalar(int(y), kind)
    return validate_scalar(y, kind)


def generate_values(
    seed: int,
    step: int,
    points: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[tuple[str, str], Scalar]:
    """Evaluate one non-reentrant period in dependency order."""
    calculated: dict[tuple[str, str], Scalar] = {}
    pending = dict(points)
    while pending:
        ready = []
        for key, point in pending.items():
            model = point.get("model")
            if not isinstance(model, Mapping):
                raise GeneratorError(f"invalid model configuration for {key}")
            if model.get("kind") != "static" or (
                key[0], str(model.get("input_point", ""))
            ) in calculated:
                ready.append((key, point))
        if not ready:
            raise GeneratorError("unresolvable static model dependency")
        for (device_id, point_id), point in ready:
            model = point["model"]
            kind = model.get("kind")
            data_type = str(point["data_type"])
            if kind == "random":
                value = random_value(
                    seed, device_id, point_id, step,
                    {**model, "data_type": data_type},
                )
            elif kind == "static":
                value = static_value(
                    {**model, "data_type": data_type},
                    {name: result for (device, name), result in calculated.items()
                     if device == device_id},
                )
            else:
                raise GeneratorError(f"unsupported model kind: {kind}")
            calculated[(device_id, point_id)] = validate_scalar(value, data_type)
            del pending[(device_id, point_id)]
    return calculated
