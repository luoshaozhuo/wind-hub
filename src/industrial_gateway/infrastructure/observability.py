"""Bounded Gateway events and periodic nine-metric snapshots."""

from __future__ import annotations

import asyncio
import math
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4


_COUNTERS = (
    "update_batches_total",
    "updated_variables_total",
    "client_writes_total",
    "forward_accepted_total",
    "forward_unknown_total",
)
_UNITS = {
    "update_batches_total": "batches",
    "updated_variables_total": "variables",
    "client_writes_total": "writes",
    "forward_accepted_total": "submissions",
    "forward_unknown_total": "submissions",
    "updated_variables_rate_per_second": "variables/s",
    "update_latency_ms_p99": "ms",
    "control_events_pending": "events",
    "metrics_covered_ms": "ms",
}


@dataclass(frozen=True, slots=True)
class Observation:
    moment: float
    gateway_id: str
    metric: str
    value: float


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    severity: str
    source: str
    error_code: str
    message: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class MetricRecord:
    name: str
    value: float
    unit: str
    window_ms: int
    sampled_at: datetime


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    metrics: tuple[MetricRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class EventPage:
    events: list[EventRecord]
    next_page_token: str = ""


class GatewayObservability:
    """Keep process-epoch counters, a sliding sample window and bounded events."""

    def __init__(self, *, window_seconds: int, interval_ms: int) -> None:
        self.window_seconds = window_seconds
        self.interval_ms = interval_ms
        self.started = monotonic()
        self.counters: Counter[tuple[str, str]] = Counter()
        self.samples: deque[Observation] = deque()
        self.events: deque[EventRecord] = deque(maxlen=4096)
        self._snapshots: dict[str, tuple[MetricRecord, ...]] = {}
        self._last_snapshot_at = 0.0
        self._task: asyncio.Task[None] | None = None
        self._pending: dict[str, int] = {}
        self._gateways: Callable[[], tuple[str, ...]] = lambda: ()

    def record(self, gateway_id: str, metric: str, value: int = 1) -> None:
        """Increment a counter at its actual business completion boundary."""
        if metric not in _COUNTERS:
            raise ValueError(f"unsupported metric: {metric}")
        self.counters[gateway_id, metric] += value
        self.samples.append(Observation(monotonic(), gateway_id, metric, value))
        self._trim()

    def latency(self, gateway_id: str, duration_ms: float) -> None:
        """Record the completed gRPC update batch response time."""
        self.samples.append(
            Observation(monotonic(), gateway_id, "update_latency_ms_p99", duration_ms)
        )
        self._trim()

    def pending(self, gateway_id: str, count: int) -> None:
        self._pending[gateway_id] = count

    def event(
        self,
        source: str,
        message: str,
        *,
        severity: str = "INFO",
        error_code: str = "",
    ) -> None:
        """Record bounded, process-local evidence, not durable command audit."""
        self.events.append(
            EventRecord(
                event_id=uuid4().hex,
                severity=severity,
                source=source,
                error_code=error_code,
                message=message,
                timestamp=datetime.now(UTC),
            )
        )

    def _trim(self) -> None:
        boundary = monotonic() - self.window_seconds
        while self.samples and self.samples[0].moment <= boundary:
            self.samples.popleft()

    def _build(self, scope: str, sampled_at: datetime) -> tuple[MetricRecord, ...]:
        now = monotonic()
        covered = min(self.window_seconds, now - self.started)
        if covered <= 0:
            return ()
        observations = [
            item
            for item in self.samples
            if (not scope or item.gateway_id == scope) and item.moment > now - self.window_seconds
        ]
        values: dict[str, float] = {}
        for name in _COUNTERS:
            values[name] = float(
                sum(
                    count
                    for (gateway, metric), count in self.counters.items()
                    if metric == name and (not scope or gateway == scope)
                )
            )
        successful = sum(
            item.value for item in observations if item.metric == "updated_variables_total"
        )
        values["updated_variables_rate_per_second"] = successful / covered
        times = sorted(
            item.value for item in observations if item.metric == "update_latency_ms_p99"
        )
        if times:
            values["update_latency_ms_p99"] = times[math.ceil(0.99 * len(times)) - 1]
        values["control_events_pending"] = float(
            sum(count for gateway, count in self._pending.items() if not scope or gateway == scope)
        )
        values["metrics_covered_ms"] = covered * 1000
        metrics: list[MetricRecord] = []
        for name, value in values.items():
            metrics.append(
                MetricRecord(
                    name=name,
                    value=value,
                    unit=_UNITS[name],
                    window_ms=min(self.window_seconds * 1000, int(covered * 1000)),
                    sampled_at=sampled_at,
                )
            )
        return tuple(metrics)

    async def _sample(self) -> None:
        while True:
            await asyncio.sleep(self.interval_ms / 1000)
            self._trim()
            sampled_at = datetime.now(UTC)
            scopes = ("", *self._gateways())
            updated = {scope: self._build(scope, sampled_at) for scope in scopes}
            self._snapshots = updated
            self._last_snapshot_at = monotonic()

    def start(self, gateway_ids: Callable[[], tuple[str, ...]]) -> None:
        """Sample the dynamic gateway set without restarting the metrics epoch."""
        if self._task is not None:
            raise RuntimeError("metrics sampler already running")
        self._gateways = gateway_ids
        self._task = asyncio.create_task(self._sample())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @property
    def ready(self) -> bool:
        return (
            bool(self._snapshots)
            and self._task is not None
            and not self._task.done()
            and monotonic() - self._last_snapshot_at <= max(3 * self.interval_ms / 1000, 1.0)
        )

    def metrics(self, scope: str, window_ms: int) -> MetricsSnapshot:
        """Return only a completed periodic snapshot, never fabricate zero data."""
        if window_ms and window_ms != self.window_seconds * 1000:
            raise ValueError("requested window_ms differs from configured window")
        if not self.ready or scope not in self._snapshots:
            return MetricsSnapshot()
        return MetricsSnapshot(metrics=self._snapshots[scope])

    def query_events(
        self,
        *,
        filter_text: str = "",
        page_size: int = 100,
        page_token: str = "",
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> EventPage:
        """Filter and paginate local event data without a wire dependency."""
        try:
            offset = int(page_token) if page_token else 0
        except ValueError as exc:
            raise ValueError("invalid page_token") from exc
        if offset < 0:
            raise ValueError("invalid page_token")
        minimum = from_time.timestamp() if from_time is not None else float("-inf")
        maximum = to_time.timestamp() if to_time is not None else float("inf")
        selected = [
            item
            for item in self.events
            if minimum <= item.timestamp.timestamp() <= maximum
            and (
                not filter_text
                or filter_text in item.source
                or filter_text in item.error_code
                or filter_text in item.message
            )
        ]
        selected.reverse()
        size = min(page_size or 100, 500)
        end = offset + size
        return EventPage(
            events=selected[offset:end],
            next_page_token=str(end) if end < len(selected) else "",
        )
