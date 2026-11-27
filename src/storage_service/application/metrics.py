"""Process-local Storage counters with lock-protected snapshots.

Persistent history remains authoritative in TDengine; these gauges track only
the current process lifetime and must not be interpreted as all-time row totals.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class StorageMetricSnapshot:
    persisted_points: int
    write_unknown: int
    schema_conflicts: int
    confirmed_records: int = 0
    persisted_rows: int = 0
    buffer_rows: int = 0
    buffer_bytes: int = 0


@dataclass(frozen=True, slots=True)
class StorageRuntimeEvent:
    event_id: int
    timestamp: datetime
    severity: str
    error_code: str
    message: str


class StorageRuntimeMetrics:
    """Thread-safe counters for event-loop and TDengine thread-pool callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._persisted_points = 0
        self._persisted_rows = 0
        self._write_unknown = 0
        self._schema_conflicts = 0
        self._confirmed_records = 0
        self._buffer_rows = 0
        self._buffer_bytes = 0
        self._next_event_id = 0
        self._events: deque[StorageRuntimeEvent] = deque(maxlen=256)

    def record_persisted(self, count: int) -> None:
        if count < 0:
            raise ValueError("persistent point counter cannot decrease")
        with self._lock:
            self._persisted_points += count

    def record_wide_persisted(self, *, rows: int, points_per_row: int) -> None:
        """Count actual physical rows and point values without confusing the units."""
        if rows < 0 or points_per_row < 1:
            raise ValueError("wide write metrics require nonnegative rows and points")
        with self._lock:
            self._persisted_rows += rows
            self._persisted_points += rows * points_per_row

    @property
    def persisted_rows(self) -> int:
        with self._lock:
            return self._persisted_rows

    def record_confirmed_records(self, count: int) -> None:
        """Count Kafka records only after an acknowledged offset commit."""
        if type(count) is not int or count < 0:
            raise ValueError("confirmed Kafka record count must be nonnegative")
        with self._lock:
            self._confirmed_records += count

    def change_buffer(self, *, rows: int, byte_count: int) -> None:
        """Track bounded admitted Kafka records across concurrent partitions."""
        if type(rows) is not int or type(byte_count) is not int:
            raise ValueError("buffer counters must be integers")
        with self._lock:
            if self._buffer_rows + rows < 0 or self._buffer_bytes + byte_count < 0:
                raise RuntimeError("Storage buffer counters cannot become negative")
            self._buffer_rows += rows
            self._buffer_bytes += byte_count

    def _event(self, *, error_code: str, message: str) -> None:
        """Called with the metrics lock held; retain bounded process-local evidence."""
        self._next_event_id += 1
        self._events.append(
            StorageRuntimeEvent(
                event_id=self._next_event_id,
                timestamp=datetime.now(UTC),
                severity="WARNING",
                error_code=error_code,
                message=message,
            )
        )

    def record_unknown(self) -> None:
        with self._lock:
            self._write_unknown += 1
            self._event(
                error_code="WRITE_UNKNOWN",
                message="TDengine write outcome has not been confirmed",
            )

    def record_schema_conflict(self) -> None:
        with self._lock:
            self._schema_conflicts += 1
            self._event(
                error_code="SCHEMA_CONFLICT",
                message="Storage rejected a conflicting physical or historical schema",
            )

    def events(self) -> tuple[StorageRuntimeEvent, ...]:
        """A snapshot of retained warnings, never an all-time event history."""
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> StorageMetricSnapshot:
        with self._lock:
            return StorageMetricSnapshot(
                persisted_points=self._persisted_points,
                write_unknown=self._write_unknown,
                schema_conflicts=self._schema_conflicts,
                confirmed_records=self._confirmed_records,
                persisted_rows=self._persisted_rows,
                buffer_rows=self._buffer_rows,
                buffer_bytes=self._buffer_bytes,
            )
