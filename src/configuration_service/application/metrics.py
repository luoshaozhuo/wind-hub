"""Bounded in-memory RPC throughput and latency measurements."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RpcMeasurement:
    request_count: int
    failed_count: int
    rate_per_second: float
    average_latency_ms: float
    p95_latency_ms: float
    window_ms: int


class RpcMetrics:
    """Track a bounded rolling observation window without storing request contents."""

    def __init__(self, *, max_samples: int = 100_000, retention_seconds: int = 3600) -> None:
        if max_samples < 1 or retention_seconds < 1:
            raise ValueError("positive RPC metric limits required")
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, float, bool]] = deque(maxlen=max_samples)
        self._retention = retention_seconds
        self._start = time.monotonic()

    def observe(self, *, duration_ms: float, success: bool) -> None:
        if not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError("invalid RPC duration")
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, duration_ms, success))
            self._discard_old(now)

    def _discard_old(self, now: float) -> None:
        threshold = now - self._retention
        while self._samples and self._samples[0][0] < threshold:
            self._samples.popleft()

    def snapshot(self, *, window_ms: int) -> RpcMeasurement:
        now = time.monotonic()
        effective_ms = (
            min(window_ms, self._retention * 1000) if window_ms > 0 else self._retention * 1000
        )
        threshold = now - effective_ms / 1000
        with self._lock:
            self._discard_old(now)
            values = [
                (duration, success)
                for observed, duration, success in self._samples
                if observed >= threshold
            ]
        count = len(values)
        failures = sum(1 for _duration, success in values if not success)
        durations = sorted(duration for duration, _success in values)
        average = sum(durations) / count if count else 0.0
        p95 = durations[math.ceil(count * 0.95) - 1] if count else 0.0
        elapsed = min(effective_ms / 1000, max(now - self._start, 0.001))
        return RpcMeasurement(
            request_count=count,
            failed_count=failures,
            rate_per_second=count / elapsed,
            average_latency_ms=average,
            p95_latency_ms=p95,
            window_ms=effective_ms,
        )
