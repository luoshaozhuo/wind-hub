"""Validated TDengine database retention and database-level DDL for wide rows."""

from __future__ import annotations

import re
from dataclasses import dataclass

from storage_service.domain.naming import StorageObjectNames


_IDENTIFIER = re.compile(r"(?:wh|st|tb)_(?:[0-9a-f]{24}|[a-z0-9_]+_[0-9a-f]{16})\Z")
_DURATION = re.compile(r"[1-9][0-9]*d\Z")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("TDengine identifier is not a generated Storage name")
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


