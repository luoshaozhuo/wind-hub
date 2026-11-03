"""Bounded CSV replay with stable source-line ordering and monotonic deadlines."""

from __future__ import annotations

import asyncio
import csv
import math
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from typing import TypeVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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


FIELDS = ("device_id", "point_id", "source_timestamp", "value", "quality", "record_id")


_T = TypeVar("_T")


async def _owned_thread(function: Callable[..., _T], *args: object) -> _T:
    """Retain a worker's lifetime after caller cancellation before removing its files."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Stop may report pending while the source retains this worker's ownership.
        await asyncio.shield(task)
        raise


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


def _index_csv(
    source: Path,
    database: Path,
    start: datetime,
    end: datetime,
    max_records: int,
    point_types: Mapping[tuple[str, str], str],
) -> None:
    """Keep repeated samples while requiring unique source record identifiers."""
    if start.tzinfo is None or end.tzinfo is None or not start < end:
        raise GeneratorError("invalid replay time window")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE records (line_no INTEGER PRIMARY KEY, ts TEXT NOT NULL,"
            " device_id TEXT NOT NULL, point_id TEXT NOT NULL, value TEXT NOT NULL,"
            " quality TEXT NOT NULL, record_id TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX replay_order ON records(ts, line_no)")
        connection.execute("CREATE UNIQUE INDEX replay_identity ON records(record_id)")
        with source.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != FIELDS:
                raise GeneratorError("unexpected replay CSV header")
            for line, row in enumerate(reader, start=2):
                if line - 1 > max_records:
                    raise GeneratorError("CSV record capacity exceeded")
                try:
                    if not all(isinstance(row.get(name), str) and row[name] for name in FIELDS):
                        raise ValueError("empty or missing field")
                    if None in row:
                        raise ValueError("unexpected additional columns")
                    stamp = datetime.fromisoformat(
                        row["source_timestamp"].replace("Z", "+00:00")
                    )
                    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
                        raise ValueError("source_timestamp must be UTC")
                    key = (row["device_id"], row["point_id"])
                    data_type = point_types.get(key)
                    if data_type is None:
                        raise GeneratorError(f"unmapped replay source point: {key}")
                    decode_value(row["value"], data_type)
                    normalized = stamp.astimezone(timezone.utc).isoformat(
                        timespec="microseconds"
                    )
                    connection.execute(
                        "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (line, normalized, row["device_id"], row["point_id"],
                         row["value"], row["quality"], row["record_id"]),
                    )
                except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                    raise GeneratorError(f"invalid CSV line {line}: {exc}") from exc


def _next_batch(
    database: Path,
    start: datetime,
    end: datetime,
    after: tuple[str, int] | None,
    size: int,
) -> list[tuple[int, str, str, str, str, str, str]]:
    beginning = start.astimezone(timezone.utc).isoformat(timespec="microseconds")
    ending = end.astimezone(timezone.utc).isoformat(timespec="microseconds")
    sql = (
        "SELECT line_no, ts, device_id, point_id, value, quality, record_id"
        " FROM records WHERE ts >= ? AND ts < ?"
    )
    parameters: tuple[object, ...] = (beginning, ending)
    if after is not None:
        sql += " AND (ts, line_no) > (?, ?)"
        parameters += after
    sql += " ORDER BY ts, line_no LIMIT ?"
    with sqlite3.connect(database) as connection:
        return list(connection.execute(sql, (*parameters, size)))


async def iter_records(
    source: Path,
    start: datetime,
    end: datetime,
    *,
    read_batch_size: int = 1000,
    max_file_bytes: int = 256 * 1024 * 1024,
    max_records: int = 1_000_000,
    point_types: Mapping[tuple[str, str], str],
) -> AsyncIterator[ReplayRecord]:
    """Bounded temporary disk index, then stable timestamp and source-line reads."""
    if not 1 <= read_batch_size <= 100_000 or max_records < 1 or max_file_bytes < 1:
        raise GeneratorError("invalid replay resource limit")
    if source.stat().st_size > max_file_bytes:
        raise GeneratorError("history file exceeds configured byte limit")
    with tempfile.TemporaryDirectory(prefix="windhub-replay-") as temporary:
        database = Path(temporary) / "replay.sqlite3"
        await _owned_thread(
            _index_csv, source, database, start, end, max_records, point_types,
        )
        cursor: tuple[str, int] | None = None
        while True:
            rows = await _owned_thread(
                _next_batch, database, start, end, cursor, read_batch_size,
            )
            if not rows:
                return
            for line_no, ts, device, point, value, quality, record_id in rows:
                cursor = (ts, line_no)
                yield ReplayRecord(
                    device, point, datetime.fromisoformat(ts),
                    value, quality, record_id, line_no,
                )


def replay_deadline(
    first: datetime,
    current: datetime,
    monotonic_start: float,
    speed: float,
) -> float:
    if not math.isfinite(speed) or speed <= 0:
        raise GeneratorError("invalid replay speed")
    return monotonic_start + (current - first).total_seconds() / speed
