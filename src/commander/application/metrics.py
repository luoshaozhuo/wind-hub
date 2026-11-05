"""Bounded in-process Commander counters and latency distributions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import ceil
from time import monotonic

_RETENTION = 300.0
_MAX_SAMPLES = 350_000


@dataclass(slots=True)
class _Series:
    total: int = 0
    samples: deque[tuple[float, float]] = field(default_factory=deque)
    evicted_at: float | None = None

    def observe(self, value: float = 1.0) -> None:
        timestamp = monotonic()
        self.total += 1
        self.samples.append((timestamp, value))
        cutoff = timestamp - _RETENTION
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        if len(self.samples) > _MAX_SAMPLES:
            removed, _ = self.samples.popleft()
            self.evicted_at = removed

    def values(self, window_ms: int) -> list[float]:
        if not 0 < window_ms <= _RETENTION * 1000:
            raise ValueError("metrics window_ms must be in (0, 300000]")
        since = monotonic() - window_ms / 1000
        if self.evicted_at is not None and self.evicted_at >= since:
            raise RuntimeError("metric window exceeds retained sample capacity")
        return [value for timestamp, value in self.samples if timestamp >= since]


class CommandMetrics:
    """One event loop owns mutations; no cross-thread synchronization required."""

    def __init__(self) -> None:
        self._accepted = _Series()
        self._completed: dict[str, _Series] = {}
        self._accept_latency = _Series()
        self._query_latency = _Series()
        self._config_failures = _Series()

    def accepted(self, count: int) -> None:
        for _ in range(count):
            self._accepted.observe()

    def completed(self, outcome: str) -> None:
        self._completed.setdefault(outcome, _Series()).observe()

    def accept_latency(self, elapsed_ms: float) -> None:
        self._accept_latency.observe(elapsed_ms)

    def query_latency(self, elapsed_ms: float) -> None:
        self._query_latency.observe(elapsed_ms)

    def config_failed(self) -> None:
        self._config_failures.observe()

    def counters(self, window_ms: int) -> dict[str, int]:
        if window_ms:
            accepted = len(self._accepted.values(window_ms))
            completed = {
                outcome: len(series.values(window_ms))
                for outcome, series in self._completed.items()
            }
            failed = len(self._config_failures.values(window_ms))
        else:
            accepted = self._accepted.total
            completed = {outcome: series.total for outcome, series in self._completed.items()}
            failed = self._config_failures.total
        result = {
            "control_accepted_total": accepted,
            "control_completed_total": sum(completed.values()),
            "config_apply_failures": failed,
        }
        result.update(
            {
                f"control_completed_total_{outcome.lower()}": count
                for outcome, count in completed.items()
            }
        )
        return result

    def latencies(self, window_ms: int) -> dict[str, float]:
        sample_window = window_ms or int(_RETENTION * 1000)
        result: dict[str, float] = {}
        for name, series in (
            ("control_accept_latency_ms_p99", self._accept_latency),
            ("result_query_latency_ms_p99", self._query_latency),
        ):
            values = sorted(series.values(sample_window))
            if values:
                result[name] = values[ceil(0.99 * len(values)) - 1]
        return result
