"""Deterministic per-point random and stateless static model evaluation."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence

from data_generator.domain.model import GeneratorError, Scalar, validate_scalar


def random_value(
    seed: int, device_id: str, point_id: str, step: int, params: Mapping[str, object],
) -> Scalar:
    """Independent PRNG streams preserve samples when unrelated points are added."""
    if step < 1:
        raise GeneratorError("step must be positive")
    raw = f"{seed}:{device_id}:{point_id}:{step}".encode()
    point_seed = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    rng = random.Random(point_seed)
    result = rng.random()
    kind = str(params.get("data_type", "float64"))
    lower = params.get("minimum", 0)
    upper = params.get("maximum", 1)
    if not isinstance(lower, (float, int)) or not isinstance(upper, (float, int)):
        raise GeneratorError("invalid bounds")
    if not math.isfinite(float(lower)) or not math.isfinite(float(upper)) or lower > upper:
        raise GeneratorError("invalid random bounds")
    if params.get("distribution", "uniform") != "uniform":
        raise GeneratorError("unsupported random distribution")
    if kind == "bool":
        value: Scalar = result >= 0.5
    elif kind.startswith(("int", "uint")):
        if type(lower) is not int or type(upper) is not int:
            raise GeneratorError("integer random bounds must be integers")
        value = rng.randint(lower, upper)
    elif kind in ("float32", "float64"):
        value = float(lower) + (float(upper) - float(lower)) * result
    else:
        raise GeneratorError(f"unsupported random scalar: {kind}")
    return validate_scalar(value, kind)


def static_value(params: Mapping[str, object], inputs: Mapping[str, Scalar]) -> Scalar:
    """Linear interpolation; no dynamic evaluation of user-defined expressions."""
    if params.get("function") != "linear_table":
        raise GeneratorError("unsupported static function")
    source = str(params.get("input_point", ""))
    raw = inputs.get(source)
    if raw is None or type(raw) not in (float, int) or not math.isfinite(float(raw)):
        raise GeneratorError("missing or invalid static model input")
    x = float(raw)
    values = params.get("values")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) < 2:
        raise GeneratorError("invalid interpolation table")
    points: list[tuple[float, float]] = []
    for item in values:
        if not isinstance(item, Sequence) or len(item) != 2:
            raise GeneratorError("invalid interpolation row")
        a, b = float(item[0]), float(item[1])
        if not math.isfinite(a) or not math.isfinite(b) or (points and a <= points[-1][0]):
            raise GeneratorError("unordered or non-finite interpolation table")
        points.append((a, b))
    policy = params.get("out_of_range", "reject")
    if policy not in ("reject", "clamp"):
        raise GeneratorError("unsupported interpolation boundary policy")
    if x < points[0][0] or x > points[-1][0]:
        if policy != "clamp":
            raise GeneratorError("input outside interpolation domain")
        x = max(points[0][0], min(points[-1][0], x))
    for (left, low), (right, high) in zip(points, points[1:]):
        if x <= right:
            return validate_scalar(low + (high-low)*(x-left)/(right-left),
                                   str(params.get("data_type", "float64")))
    return validate_scalar(points[-1][1], str(params.get("data_type", "float64")))
