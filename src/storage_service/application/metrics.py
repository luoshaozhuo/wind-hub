"""Process-local Storage counters with lock-protected snapshots.

Persistent history remains authoritative in TDengine; these gauges track only
the current process lifetime and must not be interpreted as all-time row totals.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StorageMetricSnapshot:
    persisted_points: int
    write_unknown: int
    schema_conflicts: int


class StorageRuntimeMetrics:
    """Thread-safe counters for event-loop and TDengine thread-pool callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._persisted_points = 0
        self._persisted_rows = 0
        self._write_unknown = 0
        self._schema_conflicts = 0

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

    def record_unknown(self) -> None:
        with self._lock:
            self._write_unknown += 1

    def record_schema_conflict(self) -> None:
        with self._lock:
            self._schema_conflicts += 1

    def snapshot(self) -> StorageMetricSnapshot:
        with self._lock:
            return StorageMetricSnapshot(
                persisted_points=self._persisted_points,
                write_unknown=self._write_unknown,
                schema_conflicts=self._schema_conflicts,
            )
