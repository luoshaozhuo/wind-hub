"""Bounded, weighted time-bucket histogram for approximate write-latency P99."""

from __future__ import annotations

import math
from collections import Counter, deque
from collections.abc import Iterable
from datetime import datetime
from math import ceil
from typing import TypeAlias

# Configured metrics windows are <= one hour; retain one tick for the edge.
_TICKS_PER_SECOND = 4
_LOG_BASE = math.log(1.01)
_MAX_LATENCY_MS = 3_600_000.0
_MAX_BUCKET = ceil(math.log1p(_MAX_LATENCY_MS) / _LOG_BASE)
_OVERFLOW_BUCKET = _MAX_BUCKET + 1
HistogramSnapshot: TypeAlias = tuple[tuple[int, tuple[tuple[int, int], ...]], ...]


class LatencyWindow:
    """Single-event-loop-owned histogram with bounded memory, not sampled records.

    Values up to one hour use 1% logarithmic bins, with upper-edge error
    less than 1% of (1 + latency_ms). Larger values remain detectable but
    make an overflowing P99 unavailable rather than silently underreporting.
    Windows include complete 250 ms ticks (up to 250 ms boundary uncertainty).
    """

    def __init__(self, *, window_ms: int = 60000) -> None:
        if not 0 < window_ms <= 3_600_000:
            raise ValueError("invalid latency window")
        self._max_ticks = ceil(window_ms * _TICKS_PER_SECOND / 1000) + 2
        self._ticks: deque[tuple[int, Counter[int]]] = deque()

    @staticmethod
    def _bin(latency_ms: float) -> int:
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("write latency must be finite and nonnegative")
        if latency_ms > _MAX_LATENCY_MS:
            return _OVERFLOW_BUCKET
        return int(math.log1p(latency_ms) / _LOG_BASE)

    def add(self, timestamp: datetime, latency_ms: float, *, count: int = 1) -> None:
        if count <= 0:
            raise ValueError("latency count must be positive")
        tick = int(timestamp.timestamp() * _TICKS_PER_SECOND)
        if self._ticks and tick < self._ticks[-1][0]:
            tick = self._ticks[-1][0]
        if not self._ticks or tick > self._ticks[-1][0]:
            self._ticks.append((tick, Counter()))
        self._ticks[-1][1][self._bin(latency_ms)] += count
        cutoff = tick - self._max_ticks + 1
        while self._ticks and self._ticks[0][0] < cutoff:
            self._ticks.popleft()

    def snapshot(self) -> HistogramSnapshot:
        return tuple((tick, tuple(sorted(counts.items()))) for tick, counts in self._ticks)

    def absorb_snapshot(self, snapshot: HistogramSnapshot) -> None:
        """Merge a retired ownership epoch without retaining unbounded histories."""
        merged: dict[int, Counter[int]] = {tick: Counter(counts) for tick, counts in self._ticks}
        for tick, counts in snapshot:
            merged.setdefault(tick, Counter()).update(dict(counts))
        newest = max(merged, default=0)
        self._ticks = deque(
            (tick, merged[tick]) for tick in sorted(merged) if tick >= newest - self._max_ticks + 1
        )

    def absorb(self, other: LatencyWindow) -> None:
        self.absorb_snapshot(other.snapshot())

    @staticmethod
    def window_bins(
        snapshots: Iterable[HistogramSnapshot], *, now: datetime, window_ms: int
    ) -> Counter[int]:
        cutoff = int((now.timestamp() - window_ms / 1000) * _TICKS_PER_SECOND)
        latest = int(now.timestamp() * _TICKS_PER_SECOND)
        counts: Counter[int] = Counter()
        for snapshot in snapshots:
            for tick, bins in snapshot:
                if cutoff <= tick <= latest:
                    counts.update(dict(bins))
        return counts

    @staticmethod
    def percentile_bins(counts: Counter[int]) -> float | None:
        total = counts.total()
        if total == 0:
            return None
        target = ceil(total * 0.99)
        cumulative = 0
        for bucket, count in sorted(counts.items()):
            cumulative += count
            if cumulative >= target:
                if bucket == _OVERFLOW_BUCKET:
                    return None
                return math.expm1((bucket + 1) * _LOG_BASE)
        raise RuntimeError("latency histogram has no percentile")

    @staticmethod
    def percentile(
        snapshots: Iterable[HistogramSnapshot], *, now: datetime, window_ms: int
    ) -> float | None:
        counts = LatencyWindow.window_bins(snapshots, now=now, window_ms=window_ms)
        return LatencyWindow.percentile_bins(counts)
