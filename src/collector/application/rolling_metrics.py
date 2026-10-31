"""Bounded rolling metric counters with monotonic time slots."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import RLock


class RollingMetricCounters:
    """Keep only slots required by the longest configured window."""

    def __init__(self, windows: tuple[int, ...], resolution: int) -> None:
        if (
            not windows
            or resolution <= 0
            or any(window <= 0 or window % resolution for window in windows)
        ):
            raise ValueError("invalid metric rolling-window configuration")
        self._windows = tuple(sorted(set(windows)))
        self._resolution = resolution
        self._capacity = max(self._windows) // resolution + 1
        self._slots: deque[tuple[int, dict[str, float]]] = deque(maxlen=self._capacity)
        self._lock = RLock()

    def add(self, metric: str, count: float = 1.0) -> None:
        if count < 0:
            raise ValueError("metric increment must be nonnegative")
        slot = int(time.monotonic() // self._resolution)
        with self._lock:
            if not self._slots or self._slots[-1][0] != slot:
                self._slots.append((slot, defaultdict(float)))
            self._slots[-1][1][metric] += count

    def snapshot(self, window: int) -> dict[str, float]:
        if window not in self._windows:
            raise ValueError("unsupported metric window")
        now_slot = int(time.monotonic() // self._resolution)
        lower = now_slot - window // self._resolution + 1
        values: dict[str, float] = defaultdict(float)
        with self._lock:
            for slot, data in self._slots:
                if slot < lower or slot > now_slot:
                    continue
                for key, value in data.items():
                    values[key] += value
        return dict(values)
