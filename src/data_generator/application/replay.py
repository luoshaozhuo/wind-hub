"""Bounded, stable CSV replay with temporary on-disk sort and monotonic timing."""

from __future__ import annotations

import asyncio
import csv
import math
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from data_generator.domain.model import GeneratorError


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    device_id: str
    point_id: str
    source_time: datetime
    value: str
    quality: str
    record_id: str


FIELDS = ("device_id", "point_id", "source_timestamp", "value", "quality", "record_id")


def _index_csv(
    source: Path, database: Path, start: datetime, end: datetime, max_records: int,
) -> None:
    if not start < end or start.tzinfo is None or end.tzinfo is None:
        raise GeneratorError("invalid replay time window")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE records (record_id TEXT PRIMARY KEY, ts TEXT NOT NULL,"
            " device_id TEXT NOT NULL, point_id TEXT NOT NULL, value TEXT NOT NULL,"
            " quality TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX replay_order ON records(ts, record_id)")
        with source.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != FIELDS:
                raise GeneratorError("unexpected replay CSV header")
            for line, row in enumerate(reader, 2):
                if line > max_records + 1:
                    raise GeneratorError("CSV record capacity exceeded")
                try:
                    if None in row.values() or not all(
                        row.get(field) for field in FIELDS
                    ):
                        raise ValueError("missing field")
                    stamp = datetime.fromisoformat(
                        row["source_timestamp"].replace("Z", "+00:00")
                    )
                    if stamp.tzinfo is None:
                        raise ValueError("timestamp missing timezone")
                    stamp = stamp.astimezone(timezone.utc)
                    normalized = stamp.isoformat(timespec="microseconds")
                    # Unique record ID is enforced across the entire source file.
                    connection.execute(
                        "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)",
                        (row["record_id"], normalized, row["device_id"],
                         row["point_id"], row["value"], row["quality"]),
                    )
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                    raise GeneratorError(f"invalid CSV line {line}: {exc}") from exc


def _next_batch(
    database: Path, start: datetime, end: datetime,
    after: tuple[str, str] | None, size: int,
) -> list[tuple[str, str, str, str, str, str]]:
    start_s = start.astimezone(timezone.utc).isoformat(timespec="microseconds")
    end_s = end.astimezone(timezone.utc).isoformat(timespec="microseconds")
    with sqlite3.connect(database) as connection:
        if after is None:
            query = (
                "SELECT record_id, ts, device_id, point_id, value, quality"
                " FROM records WHERE ts >= ? AND ts < ?"
                " ORDER BY ts, record_id LIMIT ?"
            )
            params: tuple[object, ...] = (start_s, end_s, size)
        else:
            query = (
                "SELECT record_id, ts, device_id, point_id, value, quality"
                " FROM records WHERE ts >= ? AND ts < ?"
                " AND (ts, record_id) > (?, ?)"
                " ORDER BY ts, record_id LIMIT ?"
            )
            params = (start_s, end_s, after[0], after[1], size)
        return list(connection.execute(query, params))


async def iter_records(
    source: Path,
    start: datetime,
    end: datetime,
    *,
    read_batch_size: int = 1000,
    max_file_bytes: int = 256 * 1024 * 1024,
    max_records: int = 1_000_000,
) -> AsyncIterator[ReplayRecord]:
    """Index on disk, then stream in timestamp/record-ID order with fixed-size reads."""
    if not 1 <= read_batch_size <= 100_000:
        raise GeneratorError("invalid replay read batch size")
    if source.stat().st_size > max_file_bytes:
        raise GeneratorError("history file exceeds capacity")
    with tempfile.TemporaryDirectory(prefix="windhub-replay-") as temporary:
        database = Path(temporary) / "records.sqlite3"
        await asyncio.to_thread(_index_csv, source, database, start, end, max_records)
        cursor: tuple[str, str] | None = None
        while True:
            rows = await asyncio.to_thread(
                _next_batch, database, start, end, cursor, read_batch_size,
            )
            if not rows:
                return
            for record_id, ts, device_id, point_id, value, quality in rows:
                cursor = (ts, record_id)
                yield ReplayRecord(
                    device_id, point_id, datetime.fromisoformat(ts),
                    value, quality, record_id,
                )


def replay_deadline(
    first: datetime, current: datetime, monotonic_start: float, speed: float,
) -> float:
    if not math.isfinite(speed) or speed <= 0:
        raise GeneratorError("invalid replay speed")
    return monotonic_start + (current - first).total_seconds() / speed
