"""Immutable domain values and strict scalar encoding rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

Scalar: TypeAlias = bool | int | float | str | bytes


class GeneratorError(ValueError):
    """A configuration or runtime contract violation."""


@dataclass(frozen=True, slots=True)
class DataPoint:
    device_id: str
    point_id: str
    value: Scalar
    source_time: datetime


@dataclass(frozen=True, slots=True)
class DataBatch:
    source_id: str
    sequence: int
    produced_at: datetime
    points: tuple[DataPoint, ...]

    def __post_init__(self) -> None:
        if not self.points:
            raise GeneratorError("empty batch")


@dataclass(frozen=True, slots=True)
class OutputTarget:
    gateway_id: str
    variable_id: str

    def __post_init__(self) -> None:
        if not self.gateway_id or not self.variable_id:
            raise GeneratorError("empty gateway or variable ID")


@dataclass(frozen=True, slots=True)
class OutputMapping:
    device_id: str
    point_id: str
    target: OutputTarget


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    target: OutputTarget
    state: str
    error_code: str = ""
    message: str = ""


def validate_scalar(value: Scalar, data_type: str, minimum: float | None = None,
                    maximum: float | None = None) -> Scalar:
    """Validate without allowing bool to masquerade as int or non-finite floats."""
    types = {
        "bool": bool, "float64": float, "float32": float,
        "int32": int, "int64": int, "uint32": int, "uint64": int,
        "string": str, "bytes": bytes,
    }
    if data_type not in types:
        raise GeneratorError(f"unsupported scalar type: {data_type}")
    expected = types[data_type]
    if expected is float:
        if type(value) not in (float, int) or not math.isfinite(float(value)):
            raise GeneratorError("non-finite or nonnumeric scalar")
        value = float(value)
    elif type(value) is not expected:
        raise GeneratorError(f"invalid value for {data_type}")
    if data_type.startswith("uint") and int(value) < 0:
        raise GeneratorError("negative unsigned value")
    bounds = {"int32": (-2**31, 2**31-1), "int64": (-2**63, 2**63-1),
              "uint32": (0, 2**32-1), "uint64": (0, 2**64-1)}
    if data_type in bounds and not bounds[data_type][0] <= int(value) <= bounds[data_type][1]:
        raise GeneratorError("integer outside scalar type range")
    if minimum is not None and float(value) < minimum:
        raise GeneratorError("scalar below minimum")
    if maximum is not None and float(value) > maximum:
        raise GeneratorError("scalar above maximum")
    return value
