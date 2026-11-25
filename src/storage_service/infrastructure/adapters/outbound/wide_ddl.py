"""Render validated TDengine wide-table DDL from immutable physical manifests.

All SQL identifiers are internally derived; TAG string literals are checked for
length and escaped. DDL is generated once per physical schema, never per point.
"""

from __future__ import annotations

import re

from storage_service.domain.wide_schema import WideSchema

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid generated TDengine identifier")
    if len(value.encode("ascii")) > 192:
        raise ValueError("TDengine identifier exceeds candidate limit")
    return value


def _tag_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("TAG identity must be nonempty")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError("TAG identity exceeds declared VARCHAR bound")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("TAG identity contains control characters")
    return "'" + value.replace("'", "''") + "'"


def stable_ddl(schema: WideSchema, *, database: str) -> str:
    """Create one entire physical table family, including the quality bitmap."""
    db = _identifier(database)
    stable = _identifier(schema.supertable_name())
    if not schema.columns or schema.quality_bytes < 1:
        raise ValueError("wide schema requires physical points")
    columns = ["ts TIMESTAMP"]
    columns.extend(
        f"{_identifier(column.column_name)} {column.sql_type}"
        for column in schema.columns
    )
    columns.append(f"quality VARBINARY({schema.quality_bytes})")
    tags = (
        "device_id VARCHAR(128), "
        "device_type VARCHAR(64), "
        "point_table_id VARCHAR(192), "
        "data_level TINYINT, "
        "schema_fingerprint VARCHAR(64)"
    )
    return (
        f"CREATE STABLE IF NOT EXISTS {db}.{stable} "
        f"({', '.join(columns)}) TAGS ({tags})"
    )


def subtable_ddl(
    schema: WideSchema,
    *,
    database: str,
    device_id: str,
    device_type: str,
) -> str:
    """Bind full immutable identity into the child table's immutable TAG set."""
    db = _identifier(database)
    table = _identifier(schema.subtable_name(device_id))
    stable = _identifier(schema.supertable_name())
    level = {"raw": 1, "protocol": 2, "business": 3}[schema.data_level.value]
    if _HEX.fullmatch(schema.fingerprint) is None:
        raise ValueError("invalid physical schema fingerprint")
    tags = (
        _tag_text(device_id, maximum=128),
        _tag_text(device_type, maximum=64),
        _tag_text(schema.point_table_id, maximum=192),
        str(level),
        _tag_text(schema.fingerprint, maximum=64),
    )
    return (
        f"CREATE TABLE IF NOT EXISTS {db}.{table} "
        f"USING {db}.{stable} TAGS ({', '.join(tags)})"
    )
