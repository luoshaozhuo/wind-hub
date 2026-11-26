"""Validate complete device records against an immutable physical wide schema.

This module prepares one atomic row; it never merges partial point updates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.domain.wide_schema import WideSchema

_INTEGERS = {
    "TINYINT": (-128, 127),
    "SMALLINT": (-32768, 32767),
    "INT": (-(2**31), 2**31 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
    "TINYINT UNSIGNED": (0, 255),
    "SMALLINT UNSIGNED": (0, 65535),
    "INT UNSIGNED": (0, 2**32 - 1),
    "BIGINT UNSIGNED": (0, 2**64 - 1),
}
_BINARY = re.compile(r"[0-9a-f]*\Z")


@dataclass(frozen=True, slots=True)
class BoundWideRow:
    timestamp_us: int
    values: tuple[int | float | bool | str | bytes | None, ...]
    quality_bitmap: bytes


def bind_wide_row(record: WideRecord, schema: WideSchema) -> BoundWideRow:
    """Bind every column in physical order without coercion or field patching."""
    if record.data_level != schema.data_level:
        raise ValueError("wide-row data level differs from physical schema")
    if record.point_ids != tuple(column.point_id for column in schema.columns):
        raise ValueError("wide-row point set/order differs from physical schema")
    if len(record.quality_bitmap) != schema.quality_bytes:
        raise ValueError("wide-row quality bitmap length differs from physical schema")
    values: list[int | float | bool | str | bytes | None] = []
    for column, source_value in zip(schema.columns, record.values, strict=True):
        value: int | float | bool | str | bytes | None = source_value
        sql_type = column.sql_type
        if value is None:
            values.append(None)
            continue
        if sql_type == "BOOL":
            if type(value) is not bool:
                raise ValueError(f"{column.point_id}: expected BOOL")
        elif sql_type in _INTEGERS:
            lower, upper = _INTEGERS[sql_type]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{column.point_id}: integer outside physical type")
        elif sql_type in ("FLOAT", "DOUBLE"):
            if not (type(value) is float or type(value) is int):
                raise ValueError(f"{column.point_id}: expected numeric")
            if type(value) is int and abs(value) > (2**24 if sql_type == "FLOAT" else 2**53):
                raise ValueError(f"{column.point_id}: integer cannot be represented exactly")
            if sql_type == "FLOAT" and abs(value) > 3.4028234663852886e38:
                raise ValueError(f"{column.point_id}: FLOAT overflow")
        elif sql_type.startswith("VARCHAR("):
            if type(value) is not str:
                raise ValueError(f"{column.point_id}: expected UTF-8 text")
            width = int(sql_type[8:-1])
            if len(value.encode("utf-8")) > width:
                raise ValueError(f"{column.point_id}: VARCHAR width exceeded")
        elif sql_type.startswith("VARBINARY("):
            width = int(sql_type[10:-1])
            if record.data_level is WideDataLevel.RAW:
                if type(value) is not str or _BINARY.fullmatch(value) is None:
                    raise ValueError(f"{column.point_id}: expected original RAW hex")
                if len(value) % 2:
                    raise ValueError(f"{column.point_id}: odd RAW hex length")
                value = bytes.fromhex(value)
            if type(value) is not bytes or len(value) > width:
                raise ValueError(f"{column.point_id}: VARBINARY width exceeded")
        else:
            raise ValueError(f"unsupported schema type: {sql_type}")
        values.append(value)
    return BoundWideRow(
        timestamp_us=record.sample_time_us,
        values=tuple(values),
        quality_bitmap=record.quality_bitmap,
    )
