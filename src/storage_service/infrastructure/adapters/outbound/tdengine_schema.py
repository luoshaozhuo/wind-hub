"""Storage v1 physical TDengine schema; all identifiers are generated internally."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from storage_service.domain.naming import StorageObjectNames

_COLUMNS = (
    ("ts", "TIMESTAMP"),
    ("sample_key", "VARCHAR(64) COMPOSITE KEY"),
    ("logical_key", "VARCHAR(64)"),
    ("content_sha256", "VARCHAR(64)"),
    ("sample_id", "VARCHAR(256)"),
    ("point_id", "VARCHAR(256)"),
    ("business_point_id", "VARCHAR(256)"),
    ("data_level", "TINYINT"),
    ("value_kind", "TINYINT"),
    ("value_i64", "BIGINT"),
    ("value_u64", "UBIGINT"),
    ("value_f64", "DOUBLE"),
    ("value_bool", "BOOL"),
    ("value_text", "VARCHAR(2048)"),
    ("payload_bytes", "VARBINARY(4096)"),
    ("quality", "TINYINT"),
    ("publish_time", "TIMESTAMP"),
    ("receive_time", "TIMESTAMP"),
)
_TAGS = (
    ("identity_hash", "VARCHAR(64)"),
    ("point_table_hash", "VARCHAR(64)"),
    ("device_hash", "VARCHAR(64)"),
    ("level_tag", "TINYINT"),
)
SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps([_COLUMNS, _TAGS, "sample-key-with-content-v1"], separators=(",", ":")).encode()
).hexdigest()

_IDENTIFIER = re.compile(r"(?:wh|st|tb)_(?:[0-9a-f]{24}|[a-z0-9_]+_[0-9a-f]{16})\\Z")
_DURATION = re.compile(r"[1-9][0-9]*d\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("TDengine identifier is not a generated Storage name")
    return value


def _digest(value: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise ValueError("expected full lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class DatabaseRetention:
    duration_days: int = 30
    keep_days: int = 1095

    def __post_init__(self) -> None:
        if self.duration_days < 1 or self.keep_days < 3 * self.duration_days:
            raise ValueError("KEEP must cover at least three DURATION intervals")

    @classmethod
    def parse(cls, *, duration: str, keep: str) -> DatabaseRetention:
        if not _DURATION.fullmatch(duration) or not _DURATION.fullmatch(keep):
            raise ValueError("retention must be positive integer days, e.g. 10d")
        return cls(duration_days=int(duration[:-1]), keep_days=int(keep[:-1]))


def create_database_sql(
    names: StorageObjectNames,
    retention: DatabaseRetention,
    *,
    cachemodel: str | None = None,
    vgroups: int = 4,
    buffer_mb: int = 256,
    replica: int = 1,
    comp: int = 2,
    wal_level: int = 2,
    wal_fsync_period_ms: int = 0,
) -> str:
    database = _identifier(names.database)
    sql = (
        f"CREATE DATABASE IF NOT EXISTS {database} PRECISION 'us' "
        f"DURATION {retention.duration_days}d KEEP {retention.keep_days}d"
    )
    if cachemodel is not None:
        if cachemodel not in {"none", "last_row", "last_value", "both"}:
            raise ValueError("unsupported TDengine CACHEMODEL")
        sql += f" CACHEMODEL '{cachemodel}'"
    if (
        type(vgroups) is not int or vgroups < 1
        or type(buffer_mb) is not int or buffer_mb < 1
        or replica != 1 or comp not in (0, 1, 2)
        or wal_level != 2 or wal_fsync_period_ms != 0
    ):
        raise ValueError("invalid database durability or allocation settings")
    return (
        f"{sql} VGROUPS {vgroups} BUFFER {buffer_mb} REPLICA {replica}"
        f" COMP {comp} WAL_LEVEL {wal_level} WAL_FSYNC_PERIOD {wal_fsync_period_ms}"
    )


def create_stable_sql(names: StorageObjectNames) -> str:
    database = _identifier(names.database)
    stable = _identifier(names.stable)
    columns = ",\n  ".join(f"{name} {kind}" for name, kind in _COLUMNS)
    tags = ",\n  ".join(f"{name} {kind}" for name, kind in _TAGS)
    return f"CREATE STABLE IF NOT EXISTS {database}.{stable} (\n  {columns}\n) TAGS (\n  {tags}\n)"


def create_table_sql(
    names: StorageObjectNames,
    *,
    identity_hash: str,
    point_table_hash: str,
    device_hash: str,
    level_tag: int,
) -> str:
    if level_tag not in (1, 2, 3):
        raise ValueError("data level must map to 1..3")
    database = _identifier(names.database)
    stable = _identifier(names.stable)
    table = _identifier(names.table)
    # All interpolated TAGs are fixed-length hashes, not user-provided SQL strings.
    tags = (_digest(identity_hash), _digest(point_table_hash), _digest(device_hash))
    return (
        f"CREATE TABLE IF NOT EXISTS {database}.{table} "
        f"USING {database}.{stable} "
        f"TAGS ('{tags[0]}', '{tags[1]}', '{tags[2]}', {level_tag})"
    )


def expected_columns() -> tuple[tuple[str, str], ...]:
    """Expose an immutable schema manifest for live information_schema comparison."""
    return _COLUMNS


def expected_tags() -> tuple[tuple[str, str], ...]:
    return _TAGS
