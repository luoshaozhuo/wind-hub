"""风机遥测短时历史：只按仿真时钟推进，重复读不更改状态。"""
from __future__ import annotations

from collections import deque


class TimeWindow:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, time_seconds: float, value: float) -> None:
        self._samples.append((time_seconds, value))
        while len(self._samples) > 1 and self._samples[1][0] <= time_seconds - self._seconds:
            self._samples.popleft()

    def average(self) -> float:
        if not self._samples:
            return 0.0
        return sum(value for _, value in self._samples) / len(self._samples)
