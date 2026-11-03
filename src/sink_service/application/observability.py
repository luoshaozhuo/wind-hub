"""Bounded process-local metrics/events for Sink Service gRPC diagnostics."""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


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

    def __init__(self, max_events: int = 10000) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._counts: dict[str, float] = {}
        self._samples: deque[tuple[datetime, dict[str, float]]] = deque(maxlen=3602)
        self.started_at = datetime.now(UTC)

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

    def sample(self, current: Mapping[str, float]) -> None:
        """Observe process and Delivery counters; keep only a bounded history."""
        now = datetime.now(UTC)
        self._samples.append((now, {**self._counts, **current}))
        while self._samples and (now - self._samples[0][0]).total_seconds() > 3600:
            self._samples.popleft()

    def snapshot(self, window_ms: int) -> dict[str, float]:
        """Return current gauges and counter differences over the requested window."""
        current = self._samples[-1][1] if self._samples else {}
        if len(self._samples) < 2:
            return dict(current)
        cutoff = datetime.now(UTC).timestamp() - window_ms / 1000
        earliest = self._samples[0][1]
        for timestamp, counters in self._samples:
            if timestamp.timestamp() <= cutoff:
                earliest = counters
            else:
                break
        values: dict[str, float] = {}
        for key, value in current.items():
            if key.endswith("_total"):
                values[key] = max(0.0, value - earliest.get(key, 0.0))
            else:
                values[key] = value
        return values

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
