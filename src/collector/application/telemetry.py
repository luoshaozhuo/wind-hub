"""Collector bounded in-memory observability; no historical persistence."""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    timestamp: datetime
    severity: str
    source: str
    error_code: str
    message: str


class CollectorTelemetry:
    """Bounded event journal and monotonic-time counters."""

    def __init__(
        self,
        *,
        event_limit: int = 1024,
        windows_seconds: tuple[int, ...] = (60, 300, 3600),
    ) -> None:
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        self.started = time.monotonic()
        if not windows_seconds or any(value <= 0 for value in windows_seconds):
            raise ValueError("metrics window lengths must be positive")
        self.windows_seconds = frozenset(windows_seconds)
        self._bucket_limit = max(windows_seconds) + 1
        self._buckets: dict[int, dict[str, int]] = {}
        self._utc_buckets: dict[int, dict[str, int]] = {}
        self._durations: dict[str, deque[tuple[float, float]]] = {}
        self._events: deque[RuntimeEvent] = deque(maxlen=event_limit)
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, tuple[float, float]] = {}

    def gauge(self, name: str, value: float) -> None:
        """Keep only measurements actually observed in the current process."""
        self._gauges[name] = (float(value), time.monotonic())

    def increment(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("counter increment cannot be negative")
        self._counters[name] = self._counters.get(name, 0) + amount
        now = int(time.monotonic())
        bucket = self._buckets.setdefault(now, {})
        bucket[name] = bucket.get(name, 0) + amount
        now_utc = int(datetime.now(UTC).timestamp())
        wall_bucket = self._utc_buckets.setdefault(now_utc, {})
        wall_bucket[name] = wall_bucket.get(name, 0) + amount
        wall_expired = now_utc - self._bucket_limit
        for key in tuple(self._utc_buckets):
            if key <= wall_expired:
                del self._utc_buckets[key]
        expired = now - self._bucket_limit
        for key in tuple(self._buckets):
            if key <= expired:
                del self._buckets[key]

    def observe_milliseconds(self, name: str, duration_ms: float) -> None:
        """Keep a bounded latency distribution with a monotonic timestamp."""
        if duration_ms < 0:
            raise ValueError("negative latency")
        observations = self._durations.setdefault(name, deque(maxlen=10000))
        now = time.monotonic()
        observations.append((now, duration_ms))
        cutoff = now - self._bucket_limit
        while observations and observations[0][0] <= cutoff:
            observations.popleft()

    def percentiles(self, window_seconds: int) -> dict[str, float]:
        """Calculate observed P95/P99 without extrapolating empty windows."""
        if window_seconds and window_seconds not in self.windows_seconds:
            raise ValueError("unsupported latency window")
        cutoff = time.monotonic() - (window_seconds or self._bucket_limit)
        result: dict[str, float] = {}
        for name, observations in self._durations.items():
            values = sorted(value for at, value in observations if at > cutoff)
            if not values:
                continue
            for fraction, label in ((0.95, "p95"), (0.99, "p99")):
                index = max(0, int(len(values) * fraction + 0.999999) - 1)
                result[f"{name}.{label}"] = values[index]
        return result

    def event(self, severity: str, source: str, error_code: str, message: str) -> None:
        self._events.append(
            RuntimeEvent(
                uuid.uuid4().hex,
                datetime.now(UTC),
                severity,
                source,
                error_code,
                message,
            )
        )

    def metrics(self) -> dict[str, float]:
        now = time.monotonic()
        values = {
            "collector.uptime_seconds": max(0.0, now - self.started),
            **{name: float(value) for name, value in self._counters.items()},
        }
        values.update(
            (name, value)
            for name, (value, sampled) in self._gauges.items()
            if now - sampled <= 15.0
        )
        return values

    def window_metrics(self, window_seconds: int) -> dict[str, float]:
        """Return actual observed window deltas; omit unobserved counters."""
        if window_seconds not in self.windows_seconds:
            raise ValueError("unsupported metrics window")
        cutoff = int(time.monotonic()) - window_seconds
        result: dict[str, float] = {}
        for second, counts in self._buckets.items():
            if second <= cutoff:
                continue
            for name, value in counts.items():
                result[name] = result.get(name, 0.0) + value
        return result

    def fixed_window(self, start_epoch: int, end_epoch: int) -> dict[str, int]:
        """Return UTC-aligned count deltas without substituting missing data."""
        if end_epoch <= start_epoch:
            raise ValueError("invalid fixed window boundaries")
        result: dict[str, int] = {}
        for second, counts in self._utc_buckets.items():
            if start_epoch <= second < end_epoch:
                for name, value in counts.items():
                    result[name] = result.get(name, 0) + value
        return result

    def events(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        filter_text: str = "",
    ) -> tuple[RuntimeEvent, ...]:
        return tuple(
            event
            for event in self._events
            if (since is None or event.timestamp >= since)
            and (until is None or event.timestamp <= until)
            and (
                not filter_text
                or filter_text in event.source
                or filter_text in event.error_code
                or filter_text in event.message
            )
        )
