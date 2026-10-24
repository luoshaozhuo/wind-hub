"""按真实时间窗口计算左保持采样的时间加权平均。"""

from __future__ import annotations

from collections import deque


class TimeWindow:
    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("window must be positive")
        self._seconds = seconds
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, time_seconds: float, value: float) -> None:
        if self._samples and time_seconds <= self._samples[-1][0]:
            raise ValueError("sample time must increase")
        self._samples.append((time_seconds, value))
        while len(self._samples) > 2 and self._samples[1][0] <= time_seconds - self._seconds:
            self._samples.popleft()

    def average(self) -> float:
        if not self._samples:
            return 0.0
        if len(self._samples) == 1:
            return self._samples[0][1]
        now = self._samples[-1][0]
        left = max(self._samples[0][0], now - self._seconds)
        area = 0.0
        for (start, value), (stop, _) in zip(self._samples, list(self._samples)[1:], strict=False):
            area += value * max(0.0, stop - max(start, left))
        duration = now - left
        return area / duration if duration > 0 else self._samples[-1][1]
