"""Bounded process-local metrics/events for Sink Service gRPC diagnostics."""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    timestamp: datetime
    severity: str
    source: str
    error_code: str
    message: str


class Observability:
    """Simple bounded counters and event ring; no persistent history is implied."""

    def __init__(
        self, max_events: int = 10000, *, window_ms: int = 60000
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._counts: dict[str, float] = {}
        self._samples: deque[tuple[datetime, dict[str, float]]] = deque(maxlen=36002)
        self.started_at = datetime.now(UTC)
        self._window_ms = window_ms
        self._computed: dict[str, float] = {'metrics_snapshot_ready': 0.0}

    def count(self, metric: str, delta: float = 1.0) -> None:
        self._counts[metric] = self._counts.get(metric, 0.0) + delta

    def event(
        self,
        severity: str,
        source: str,
        error_code: str,
        message: str,
    ) -> None:
        self._events.append(
            RuntimeEvent(
                event_id=secrets.token_hex(12),
                timestamp=datetime.now(UTC),
                severity=severity,
                source=source,
                error_code=error_code,
                message=message,
            )
        )
        self.count("events_total")

    def sample(
        self,
        current: Mapping[str, float],
        *,
        latency_observations: Sequence[tuple[datetime, float]] = (),
        latency_overflow_at: datetime | None = None,
    ) -> None:
        """Compute one immutable window snapshot; querying never recalculates."""
        now = datetime.now(UTC)
        counters = {**self._counts, **current}
        self._samples.append((now, counters))
        cutoff = now.timestamp() - self._window_ms / 1000
        while len(self._samples) > 1 and (
            self._samples[1][0].timestamp() <= cutoff
        ):
            self._samples.popleft()
        previous = (
            {}
            if cutoff <= self.started_at.timestamp()
            else self._samples[0][1]
        )
        covered_ms = min(
            float(self._window_ms), max(0.0, (now - self.started_at).total_seconds() * 1000)
        )
        result = {
            name: float(counters.get(name, 0.0))
            for name in (
                'received_total', 'delivered_total', 'failed_total',
                'dropped_total', 'dead_letter_total', 'io_inflight',
            )
        }
        if 'backlog_messages' in counters:
            result['backlog_messages'] = float(counters['backlog_messages'])
        result['metrics_snapshot_ready'] = 1.0
        result['metrics_covered_ms'] = covered_ms
        if covered_ms > 0:
            delivered = max(
                0.0, counters.get('delivered_total', 0.0)
                - previous.get('delivered_total', 0.0)
            )
            result['delivered_rate_per_second'] = delivered / (covered_ms / 1000)
        succeeded = max(
            0.0, counters.get('write_success_total', 0.0)
            - previous.get('write_success_total', 0.0)
        )
        failed = max(
            0.0, counters.get('failed_total', 0.0)
            - previous.get('failed_total', 0.0)
        )
        if succeeded + failed > 0:
            result['write_success_ratio'] = succeeded / (succeeded + failed)
        if latency_overflow_at is None or latency_overflow_at.timestamp() <= cutoff:
            values = sorted(
                latency for timestamp, latency in latency_observations
                if cutoff < timestamp.timestamp() <= now.timestamp()
            )
            if values:
                result['write_latency_ms_p99'] = values[ceil(len(values) * 0.99) - 1]
        self._computed = result

    def snapshot(self, window_ms: int) -> dict[str, float]:
        """Read only the last completed periodic snapshot."""
        if window_ms != self._window_ms:
            raise ValueError('unsupported metrics window')
        return dict(self._computed)

    def list_events(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
        query: str,
        offset: int,
        count: int,
    ) -> tuple[tuple[RuntimeEvent, ...], str]:
        values = [
            item
            for item in self._events
            if (start is None or item.timestamp >= start)
            and (end is None or item.timestamp <= end)
            and (
                not query
                or query in item.source
                or query in item.error_code
                or query in item.message
            )
        ]
        page = tuple(values[offset : offset + count])
        next_token = str(offset + count) if offset + count < len(values) else ""
        return page, next_token
