"""Single-authority Gateway variable state, independent of Protocol Server lifetime."""

from __future__ import annotations

import asyncio
import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeAlias

Scalar: TypeAlias = bool | int | float | str | bytes

_INTEGER_RANGES = {
    "int8": (-(1 << 7), (1 << 7) - 1),
    "uint8": (0, (1 << 8) - 1),
    "int16": (-(1 << 15), (1 << 15) - 1),
    "uint16": (0, (1 << 16) - 1),
    "int32": (-(1 << 31), (1 << 31) - 1),
    "uint32": (0, (1 << 32) - 1),
    "int64": (-(1 << 63), (1 << 63) - 1),
    "uint64": (0, (1 << 64) - 1),
}


@dataclass(frozen=True, slots=True)
class VariableSnapshot:
    variable_id: str
    value: Scalar | None
    version: int
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class UpdateResult:
    variable_id: str
    success: bool
    error_code: str = ""
    message: str = ""


def validate_value(data_type: str, value: Scalar) -> None:
    """Validate before publication so a successful update remains encodable."""
    if data_type == "bool":
        if type(value) is not bool:
            raise TypeError("bool requires a boolean value")
    elif data_type in _INTEGER_RANGES:
        lower, upper = _INTEGER_RANGES[data_type]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{data_type} value out of range or not integer")
    elif data_type in {"float32", "float64"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{data_type} requires a finite number")
        try:
            finite = math.isfinite(value)
        except OverflowError as exc:
            raise ValueError(f"{data_type} value out of range") from exc
        if not finite:
            raise ValueError(f"{data_type} requires a finite number")
        if data_type == "float32":
            try:
                struct.pack(">f", value)
            except (OverflowError, struct.error) as exc:
                raise ValueError("float32 out of range") from exc
    elif data_type in {"str", "string"}:
        if type(value) is not str:
            raise TypeError("string requires str")
    elif data_type == "bytes":
        if type(value) is not bytes:
            raise TypeError("bytes requires bytes")
    else:
        raise ValueError(f"unsupported Gateway data type: {data_type}")


class VariableStore:
    """Per-variable atomic publication; batches intentionally are not transactions."""

    def __init__(self, types: dict[str, str]) -> None:
        self._types = dict(types)
        self._values: dict[str, VariableSnapshot] = {}
        self._locks = {name: asyncio.Lock() for name in types}

    def seed_from(self, previous: VariableStore) -> None:
        """Retain only type-compatible values across a Server config reload."""
        for variable_id, data_type in self._types.items():
            snapshot = previous._values.get(variable_id)
            if snapshot is None or snapshot.value is None:
                continue
            try:
                validate_value(data_type, snapshot.value)
            except (TypeError, ValueError):
                continue
            self._values[variable_id] = snapshot

    def read(self, variable_id: str) -> VariableSnapshot:
        if variable_id not in self._types:
            raise KeyError(f"unknown variable: {variable_id}")
        return self._values.get(
            variable_id,
            VariableSnapshot(variable_id, None, 0, None),
        )

    async def update(self, variable_id: str, value: Scalar) -> VariableSnapshot:
        if variable_id not in self._types:
            raise KeyError(f"unknown variable: {variable_id}")
        validate_value(self._types[variable_id], value)
        async with self._locks[variable_id]:
            previous = self._values.get(variable_id)
            snapshot = VariableSnapshot(
                variable_id=variable_id,
                value=value,
                version=1 if previous is None else previous.version + 1,
                updated_at=datetime.now(UTC),
            )
            self._values[variable_id] = snapshot
            return snapshot

    async def update_many(self, values: list[tuple[str, Scalar]]) -> list[UpdateResult]:
        """Process items in order, preserving prior successes on a failed item."""
        results: list[UpdateResult] = []
        for variable_id, value in values:
            try:
                await self.update(variable_id, value)
            except KeyError as exc:
                results.append(UpdateResult(variable_id, False, "NOT_FOUND", str(exc)))
            except (TypeError, ValueError) as exc:
                results.append(UpdateResult(variable_id, False, "INVALID_VALUE", str(exc)))
            else:
                results.append(UpdateResult(variable_id, True))
        return results
