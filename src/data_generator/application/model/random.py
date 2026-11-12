"""Typed, point-table-driven random generator with deterministic per-point streams."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from typing import cast

from data_generator.application.model.common import is_real_number
from data_generator.domain.model import GeneratorError, Scalar, validate_scalar


def _numeric(value: object, label: str) -> float:
    if not is_real_number(value) or not math.isfinite(value):
        raise GeneratorError(f"random {label} must be a finite number")
    return float(value)


def _choices(rule: Mapping[str, object]) -> tuple[list[Scalar], list[float] | None]:
    values = rule.get("values")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes) or not values:
        raise GeneratorError("random choice requires candidates")
    options: list[Scalar] = [cast(Scalar, item) for item in values]
    weights = rule.get("weights")
    if weights is None:
        return options, None
    if not isinstance(weights, list) or len(weights) != len(options):
        raise GeneratorError("random choice weights do not match candidates")
    normalized = [_numeric(weight, "choice weight") for weight in weights]
    if any(weight < 0 for weight in normalized) or not sum(normalized) > 0:
        raise GeneratorError("random choice requires nonnegative and nonzero weights")
    return options, normalized


def validate_rule(rule: Mapping[str, object], data_type: str) -> None:
    """Validate a full distribution without producing an unbounded random sample."""
    distribution = rule.get("distribution")
    is_float = data_type in {"float32", "float64"}
    is_int = data_type.startswith(("int", "uint"))
    hold_steps = rule.get("hold_steps", 1)
    if type(hold_steps) is not int or hold_steps < 1:
        raise GeneratorError("hold_steps must be a positive integer")
    if distribution == "constant":
        validate_scalar(cast(Scalar, rule.get("value")), data_type)
    elif distribution == "uniform" and is_float:
        low = _numeric(rule.get("min", rule.get("minimum")), "uniform lower bound")
        high = _numeric(rule.get("max", rule.get("maximum")), "uniform upper bound")
        if low > high:
            raise GeneratorError("random uniform bounds reversed")
        validate_scalar(low, data_type)
        validate_scalar(high, data_type)
    elif distribution == "normal" and is_float:
        _numeric(rule.get("mean"), "normal mean")
        if _numeric(rule.get("std"), "normal standard deviation") < 0:
            raise GeneratorError("negative random standard deviation")
        policy = rule.get("out_of_range", "reject_sample")
        if policy not in {"reject_sample", "resample"}:
            raise GeneratorError("normal bounds need an explicit rejection policy")
        normal_bounds: dict[str, float] = {}
        for key in ("minimum", "maximum"):
            if key in rule:
                normal_bounds[key] = _numeric(rule[key], key)
        if (
            "minimum" in normal_bounds
            and "maximum" in normal_bounds
            and normal_bounds["minimum"] > normal_bounds["maximum"]
        ):
            raise GeneratorError("invalid normal bounds")
        attempts = rule.get("max_attempts")
        if policy == "resample" and (type(attempts) is not int or attempts < 1):
            raise GeneratorError("bounded normal resampling requires max_attempts")
    elif distribution == "uniform_int" and is_int:
        int_low = rule.get("min", rule.get("minimum"))
        int_high = rule.get("max", rule.get("maximum"))
        if type(int_low) is not int or type(int_high) is not int or int_low > int_high:
            raise GeneratorError("random integer range must use ordered exact integers")
        validate_scalar(int_low, data_type)
        validate_scalar(int_high, data_type)
    elif distribution == "bernoulli" and data_type == "bool":
        probability = _numeric(rule.get("probability"), "Bernoulli probability")
        if not 0 <= probability <= 1:
            raise GeneratorError("Bernoulli probability out of range")
    elif distribution == "choice" and (is_int or data_type in {"str", "bytes"}):
        options, _ = _choices(rule)
        for option in options:
            validate_scalar(option, data_type)
    else:
        raise GeneratorError(f"distribution {distribution!r} incompatible with {data_type}")


def sample(
    seed: int,
    device_id: str,
    point_id: str,
    step: int,
    rule: Mapping[str, object],
    data_type: str,
) -> Scalar:
    """Determinism is independent of other devices, rules, and evaluation order."""
    if type(seed) is not int or step < 1:
        raise GeneratorError("invalid random seed or step")
    validate_rule(rule, data_type)
    hold_steps = rule.get("hold_steps", 1)
    if type(hold_steps) is not int:
        raise GeneratorError("hold_steps must be a positive integer")
    window = (step - 1) // hold_steps
    digest = hashlib.sha256(f"{seed}:{device_id}:{point_id}:{window}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    distribution = rule["distribution"]
    if distribution == "constant":
        value = rule["value"]
    elif distribution == "uniform":
        value = rng.uniform(
            _numeric(rule.get("min", rule.get("minimum")), "uniform lower bound"),
            _numeric(rule.get("max", rule.get("maximum")), "uniform upper bound"),
        )
    elif distribution == "uniform_int":
        int_low = rule.get("min", rule.get("minimum"))
        int_high = rule.get("max", rule.get("maximum"))
        if type(int_low) is not int or type(int_high) is not int:
            raise GeneratorError("random integer range must use ordered exact integers")
        value = rng.randint(int_low, int_high)
    elif distribution == "bernoulli":
        value = rng.random() < _numeric(rule["probability"], "Bernoulli probability")
    elif distribution == "choice":
        options, weights = _choices(rule)
        value = rng.choices(options, weights=weights, k=1)[0]
    else:
        minimum_raw = rule.get("minimum", -math.inf)
        maximum_raw = rule.get("maximum", math.inf)
        if not is_real_number(minimum_raw) or not is_real_number(maximum_raw):
            raise GeneratorError("normal bounds must be numeric")
        minimum = float(minimum_raw)
        maximum = float(maximum_raw)
        attempts_raw = rule.get("max_attempts", 1)
        if type(attempts_raw) is not int:
            raise GeneratorError("max_attempts must be an integer")
        value = 0.0
        for _ in range(attempts_raw):
            value = rng.gauss(
                _numeric(rule["mean"], "normal mean"),
                _numeric(rule["std"], "normal standard deviation"),
            )
            if minimum <= value <= maximum:
                break
        else:
            raise GeneratorError("normal sample outside declared bounds")
    return validate_scalar(cast(Scalar, value), data_type)


def generate(
    step: int, points: Mapping[tuple[str, str], Mapping[str, object]]
) -> dict[tuple[str, str], Scalar]:
    result: dict[tuple[str, str], Scalar] = {}
    for (device, point), metadata in sorted(points.items()):
        rule = metadata.get("model")
        if not isinstance(rule, Mapping):
            raise GeneratorError("random point missing model rule")
        seed = rule.get("seed", 0)
        if type(seed) is not int:
            raise GeneratorError("random seed must be an integer")
        result[(device, point)] = sample(
            seed, device, point, step, rule, str(metadata["data_type"])
        )
    return result
