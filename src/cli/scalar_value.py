"""Encode JSON scalar values for Gateway updates and Commander writes."""

from __future__ import annotations

import json
import math
from typing import Any


def parse_json_scalar(raw: str) -> dict[str, Any]:
    """Preserve exact protobuf scalar kind and reject unsupported JSON values."""
    item = json.loads(raw)
    if isinstance(item, bool):
        return {"bool_value": item}
    if isinstance(item, int):
        if not -(2**63) <= item <= 2**64 - 1:
            raise ValueError("integer outside protobuf 64-bit limits")
        return {"sint64_value" if item <= 2**63 - 1 else "uint64_value": str(item)}
    if isinstance(item, float) and math.isfinite(item):
        return {"double_value": item}
    if isinstance(item, str):
        return {"string_value": item}
    raise ValueError("write value must be a finite JSON scalar")
