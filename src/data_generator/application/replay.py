"""CSV replay reader and monotonic schedule."""

from __future__ import annotations

import csv
import math
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


def load_records(path: Path, start: datetime, end: datetime) -> list[ReplayRecord]:
    """Validate all rows before dispatch; stable same-timestamp order uses record ID."""
    if not start < end or start.tzinfo is None or end.tzinfo is None:
        raise GeneratorError("invalid replay time window")
    records: list[ReplayRecord] = []
    known: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise GeneratorError("unexpected replay CSV header")
        for line, row in enumerate(reader, 2):
            try:
                if None in row.values():
                    raise ValueError("missing value")
                record_id = row["record_id"].strip()
                if not record_id or record_id in known:
                    raise ValueError("duplicate or empty record ID")
                known.add(record_id)
                stamp = datetime.fromisoformat(row["source_timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("timestamp missing timezone")
                if start <= stamp < end:
                    records.append(ReplayRecord(row["device_id"], row["point_id"],
                                                stamp.astimezone(timezone.utc),
                                                row["value"], row["quality"], record_id))
            except (ValueError, KeyError, TypeError) as exc:
                raise GeneratorError(f"invalid CSV line {line}: {exc}") from exc
    records.sort(key=lambda item: (item.source_time, item.record_id))
    return records


def replay_deadline(first: datetime, current: datetime, monotonic_start: float,
                    speed: float) -> float:
    if not math.isfinite(speed) or speed <= 0:
        raise GeneratorError("invalid replay speed")
    return monotonic_start + (current - first).total_seconds() / speed
