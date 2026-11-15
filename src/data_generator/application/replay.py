"""Pure replay record and value conversion policy; no file or database I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from data_generator.domain.model import GeneratorError, Scalar, validate_scalar


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    device_id: str
    point_id: str
    source_time: datetime
    value: str
    quality: str
    record_id: str
    line_number: int


def decode_value(raw: str, kind: str) -> Scalar:
    """One scalar conversion contract for preflight and replay emission."""
    try:
        if kind == "bool":
            if raw.lower() not in ("true", "false"):
                raise ValueError("bool must be true or false")
            value: Scalar = raw.lower() == "true"
        elif kind.startswith(("int", "uint")):
            value = int(raw)
        elif kind.startswith("float"):
            value = float(raw)
        elif kind == "bytes":
            value = raw.encode("utf-8")
        elif kind == "str":
            value = raw
        else:
            raise GeneratorError(f"unsupported replay type: {kind}")
        return validate_scalar(value, kind)
    except (ValueError, OverflowError) as exc:
        raise GeneratorError(f"invalid {kind} value: {raw!r}") from exc


def replay_deadline(
    first: datetime,
    current: datetime,
    monotonic_start: float,
    speed: float,
) -> float:
    if not math.isfinite(speed) or speed <= 0:
        raise GeneratorError("invalid replay speed")
    return monotonic_start + (current - first).total_seconds() / speed
