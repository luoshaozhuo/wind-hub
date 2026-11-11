"""Bounded process-local metrics/events for Sink Service gRPC diagnostics."""

from __future__ import annotations

import secrets
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sink_service.application.latency_window import LatencyWindow


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

    def __init__(self, max_events: int = 10000, *, window_ms: int = 60000) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._counts: dict[str, float] = {}
        self._samples: deque[tuple[datetime, dict[str, float]]] = deque(maxlen=36002)
        self.started_at = datetime.now(UTC)
        self._window_ms = window_ms
        self._computed: dict[str, float] = {"metrics_snapshot_ready": 0.0}
        self.computed_at: datetime | None = None
        self._delivery_samples: dict[str, deque[tuple[datetime, dict[str, float]]]] = {}
        self._delivery_computed: dict[str, dict[str, float]] = {}
        self._delivery_started_at: dict[str, datetime] = {}

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
                source=source[:128],
                error_code=error_code[:128],
                message=message[:1024],
            )
        )
        self.count("events_total")

    def _compute_window(
        self,
        now: datetime,
        origin: datetime,
        history: deque[tuple[datetime, dict[str, float]]],
        counters: dict[str, float],
    ) -> dict[str, float]:
        cutoff = now.timestamp() - self._window_ms / 1000
        while len(history) > 1 and history[1][0].timestamp() <= cutoff:
            history.popleft()
        previous = {} if cutoff <= origin.timestamp() else history[0][1]
        covered_ms = min(
            float(self._window_ms),
            max(0.0, (now - origin).total_seconds() * 1000),
        )
        result = {
            name: float(counters.get(name, 0.0))
            for name in (
                "received_total",
                "delivered_total",
                "failed_total",
                "dropped_total",
                "dead_letter_total",
                "io_inflight",
                "input_batches_total",
                "input_values_total",
                "acknowledged_batches_total",
                "rejected_batches_total",
                "routed_values_total",
                "retry_total",
                "dead_letter_invalid_batches_total",
            )
        }
        for name in (
            "realtime_event_loop_lag_ms_p99",
            "reliable_event_loop_lag_ms_p99",
        ):
            if name in counters:
                result[name] = float(counters[name])
        if "backlog_messages" in counters:
            result["backlog_messages"] = float(counters["backlog_messages"])
        result["metrics_snapshot_ready"] = 1.0
        result["metrics_covered_ms"] = covered_ms
        if covered_ms > 0:
            delivered = max(
                0.0,
                counters.get("delivered_total", 0.0) - previous.get("delivered_total", 0.0),
            )
            result["delivered_rate_per_second"] = delivered / (covered_ms / 1000)
        succeeded = max(
            0.0,
            counters.get("write_success_total", 0.0) - previous.get("write_success_total", 0.0),
        )
        failed = max(
            0.0,
            counters.get("failed_total", 0.0) - previous.get("failed_total", 0.0),
        )
        if succeeded + failed > 0:
            result["write_success_ratio"] = succeeded / (succeeded + failed)
        return result

    def sample(
        self,
        current: Mapping[str, float],
        *,
        process_latency_bins: Mapping[int, int] | None = None,
        delivery_counters: Mapping[str, Mapping[str, float]] | None = None,
        delivery_origins: Mapping[str, datetime] | None = None,
        delivery_p99: Mapping[str, float] | None = None,
    ) -> None:
        """Publish one periodic process + Delivery scope snapshot atomically."""
        now = datetime.now(UTC)
        counters = {**self._counts, **current}
        self._samples.append((now, counters))
        process = self._compute_window(
            now,
            self.started_at,
            self._samples,
            counters,
        )
        if process_latency_bins is not None:
            process_p99 = LatencyWindow.percentile_bins(Counter(process_latency_bins))
            if process_p99 is not None:
                process["write_latency_ms_p99"] = process_p99
        # A maximum of Delivery P99s is not the combined process P99.
        if delivery_p99:
            process["write_latency_ms_max_delivery_p99"] = max(delivery_p99.values())
        details: dict[str, dict[str, float]] = {}
        for identifier, values in (delivery_counters or {}).items():
            history = self._delivery_samples.setdefault(identifier, deque(maxlen=36002))
            snapshot = dict(values)
            history.append((now, snapshot))
            result = self._compute_window(
                now,
                self._delivery_started_at.setdefault(
                    identifier, (delivery_origins or {}).get(identifier, now)
                ),
                history,
                snapshot,
            )
            # Readiness and window coverage are process-level contract metrics.
            result.pop("metrics_snapshot_ready", None)
            result.pop("metrics_covered_ms", None)
            if delivery_p99 is not None:
                result.pop("write_latency_ms_p99", None)
                if identifier in delivery_p99:
                    result["write_latency_ms_p99"] = delivery_p99[identifier]
            details[identifier] = result
        for identifier in tuple(self._delivery_samples):
            if identifier not in details:
                del self._delivery_samples[identifier]
                self._delivery_started_at.pop(identifier, None)
        self._delivery_computed = details
        self._computed = process
        self.computed_at = now

    def reset_epoch(self) -> None:
        """Do not calculate window deltas across restarted worker processes."""
        self.started_at = datetime.now(UTC)
        self._samples.clear()
        self._delivery_samples.clear()
        self._delivery_started_at.clear()
        self._delivery_computed = {}
        self._computed = {"metrics_snapshot_ready": 0.0}
        self.computed_at = None

    def mark_unavailable(self) -> None:
        """Do not expose stale or fabricated values when a worker is unreachable."""
        self._computed = {"metrics_snapshot_ready": 0.0}
        self._delivery_computed = {}
        self.computed_at = datetime.now(UTC)

    def snapshot(self, window_ms: int) -> dict[str, float]:
        """Read the last process snapshot, without recalculating metrics."""
        if window_ms != self._window_ms:
            raise ValueError("unsupported metrics window")
        return dict(self._computed)

    def delivery_snapshot(self, window_ms: int) -> dict[str, dict[str, float]]:
        """Read the last atomically sampled per-Delivery metric collection."""
        if window_ms != self._window_ms:
            raise ValueError("unsupported metrics window")
        return {key: dict(value) for key, value in self._delivery_computed.items()}

    def list_events(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
        query: str,
        after_id: str,
        count: int,
    ) -> tuple[tuple[RuntimeEvent, ...], str]:
        events = tuple(self._events)
        offset = 0
        if after_id:
            for index, item in enumerate(events):
                if item.event_id == after_id:
                    offset = index + 1
                    break
            else:
                raise ValueError("event page cursor expired or invalid")
        values = [
            item
            for item in events[offset:]
            if (start is None or item.timestamp >= start)
            and (end is None or item.timestamp <= end)
            and (
                not query
                or query in item.source
                or query in item.error_code
                or query in item.message
            )
        ]
        page = tuple(values[:count])
        next_token = page[-1].event_id if len(values) > count else ""
        return page, next_token
