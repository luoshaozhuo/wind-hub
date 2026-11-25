"""Deterministic device-wide TDengine schema planning from exact point versions.

This module constructs immutable physical manifests, not DDL side effects.
It does not infer points from received Kafka values.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from storage_service.domain.wide_record import WideDataLevel

_SQL_TYPE = re.compile(
    r"(?:BOOL|TINYINT|SMALLINT|INT|BIGINT|FLOAT|DOUBLE)"
    r"(?: UNSIGNED)?|(?:VARCHAR|VARBINARY)\([1-9][0-9]*\)"
)
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SAFE = re.compile(r"[^a-z0-9_]+")

_FIXED_WIDTHS = {
    "BOOL": 1,
    "TINYINT": 1,
    "TINYINT UNSIGNED": 1,
    "SMALLINT": 2,
    "SMALLINT UNSIGNED": 2,
    "INT": 4,
    "INT UNSIGNED": 4,
    "BIGINT": 8,
    "BIGINT UNSIGNED": 8,
    "FLOAT": 4,
    "DOUBLE": 8,
}


def _digest(*parts: str) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _name(prefix: str, *identity: str) -> str:
    readable = _SAFE.sub("_", identity[0].lower()).strip("_")[:36]
    if not readable:
        readable = "id"
    return f"{prefix}_{readable}_{_digest(*identity)[:16]}"


def _width(sql_type: str) -> int:
    if not _SQL_TYPE.fullmatch(sql_type):
        raise ValueError(f"unsupported TDengine physical type: {sql_type}")
    if sql_type in _FIXED_WIDTHS:
        return _FIXED_WIDTHS[sql_type]
    bound = int(sql_type.split("(", 1)[1][:-1])
    if bound > 16374:
        raise ValueError("variable-width column exceeds TDengine candidate bound")
    return bound + 2


@dataclass(frozen=True, slots=True)
class PhysicalPoint:
    point_id: str
    sql_type: str


@dataclass(frozen=True, slots=True)
class PhysicalColumn:
    point_id: str
    column_name: str
    sql_type: str


@dataclass(frozen=True, slots=True)
class WideSchema:
    point_table_id: str
    point_table_version: str
    data_level: WideDataLevel
    columns: tuple[PhysicalColumn, ...]
    quality_bytes: int
    declared_row_bytes: int
    fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        point_table_id: str,
        point_table_version: str,
        data_level: WideDataLevel,
        points: tuple[PhysicalPoint, ...],
        max_row_bytes: int,
    ) -> WideSchema:
        """Make a complete schema independently of initial observed values."""
        if not point_table_id or not _VERSION.fullmatch(point_table_version):
            raise ValueError("exact PointTable identity and semantic version required")
        if not points or len({point.point_id for point in points}) != len(points):
            raise ValueError("physical PointTable must have unique nonempty point IDs")
        if any(not point.point_id for point in points):
            raise ValueError("invalid physical point ID")
        if type(max_row_bytes) is not int or max_row_bytes < 1:
            raise ValueError("max_row_bytes must be a positive integer")
        ordered = sorted(points, key=lambda point: point.point_id.encode("utf-8"))
        columns = tuple(
            PhysicalColumn(
                point_id=point.point_id,
                column_name=(
                    f"{_name('p', point.point_id, point_table_id, data_level.value)}"
                    f"_v{point_table_version.replace('.', '_')}"
                ),
                sql_type=point.sql_type,
            )
            for point in ordered
        )
        quality_bytes = (len(columns) + 3) // 4
        row_bytes = 8 + quality_bytes + 2 + sum(_width(c.sql_type) for c in columns)
        if row_bytes > max_row_bytes:
            raise ValueError(
                f"wide schema declared row width {row_bytes} exceeds limit {max_row_bytes}"
            )
        fingerprint = _digest(
            "windhub-wide-v1",
            point_table_id,
            point_table_version,
            data_level.value,
            *(f"{c.column_name}:{c.sql_type}" for c in columns),
            f"quality-2bit-lowfirst:{quality_bytes}",
        )
        return cls(
            point_table_id=point_table_id,
            point_table_version=point_table_version,
            data_level=data_level,
            columns=columns,
            quality_bytes=quality_bytes,
            declared_row_bytes=row_bytes,
            fingerprint=fingerprint,
        )

    def supertable_name(self) -> str:
        return _name(
            "st", self.point_table_id, self.point_table_version,
            self.data_level.value, self.fingerprint
        )

    def subtable_name(self, device_id: str) -> str:
        if not device_id:
            raise ValueError("device identity required")
        return _name("tb", device_id, self.fingerprint)

    @staticmethod
    def database_name(wind_farm_id: str) -> str:
        if not wind_farm_id:
            raise ValueError("wind farm identity required")
        return _name("wh", wind_farm_id)
