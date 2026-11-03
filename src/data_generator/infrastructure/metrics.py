"""Bounded window metrics without invented samples or unbounded label growth."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common

if TYPE_CHECKING:
    from data_generator.application.runtime import Runtime


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def snapshot(runtime: Runtime, scope: str, window_ms: int) -> common.GetMetricsResponse:
    """Export cumulative counters and observed-window percentiles, never fabricated P99."""
    config = runtime.settings.service.get("metrics", {})
    default_ms = int(float(config.get("window_seconds", 60)) * 1000)
    maximum_ms = int(float(config.get("retain_seconds", 3600)) * 1000)
    duration_ms = window_ms or default_ms
    if duration_ms <= 0 or duration_ms > maximum_ms:
        raise ValueError("invalid metrics window")
    if scope not in ("", "service") and not scope.startswith("gateway:"):
        raise ValueError("unsupported metrics scope")
    gateway_filter = scope.partition(":")[2] if scope.startswith("gateway:") else None
    if gateway_filter == "":
        raise ValueError("Gateway scope requires nonempty ID")
    maximum = int(config.get("max_series", 2000))
    now = time.monotonic()
    timestamp = Timestamp()
    timestamp.FromDatetime(datetime.now(timezone.utc))
    metrics: list[common.Metric] = []

    def add(name: str, value: float, unit: str, window: int = 0) -> None:
        if len(metrics) >= maximum:
            return
        metrics.append(common.Metric(
            name=name, value=float(value), unit=unit,
            window_ms=window, sampled_at=timestamp,
        ))

    if gateway_filter is None:
        for key, value in sorted(runtime.counters.items()):
            add(f"data_generator.{key}_total", float(value), "count")
        add("data_generator.active_generations", float(
            sum(state.actual_state in ("STARTING", "RUNNING", "STOPPING")
                for state in runtime.generations.values())
        ), "count")
        add("data_generator.unsafe_gateways", float(
            len(runtime.writer.unsafe_gateways)
        ), "count")
        if runtime.replay_speed is not None:
            add("data_generator.replay_speed", runtime.replay_speed, "ratio")
        if runtime.latest_replay_timestamp is not None:
            add(
                "data_generator.replay_source_timestamp",
                runtime.latest_replay_timestamp.timestamp(),
                "unix_second",
            )
        observations: dict[str, list[float]] = defaultdict(list)
        for observed_at, name, value in runtime.observations:
            if (now - observed_at) * 1000 <= duration_ms:
                observations[name].append(value)
        for name, values in sorted(observations.items()):
            for label, percentile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
                result = _percentile(values, percentile)
                if result is not None:
                    add(f"data_generator.{name}_{label}", result, "second", duration_ms)
        for gateway_id, counts in sorted(runtime.gateway_counts.items()):
            for status, count in sorted(counts.items()):
                add(f"gateway.{gateway_id}.{status.lower()}_total", count, "count")
    else:
        for status, count in sorted(runtime.gateway_counts.get(
            gateway_filter, {},
        ).items()):
            add(f"gateway.{gateway_filter}.{status.lower()}_total", count, "count")

    durations: dict[str, list[float]] = defaultdict(list)
    for observed_at, gateway_id, milliseconds in runtime.writer.rpc_durations:
        if (now - observed_at) * 1000 <= duration_ms:
            if gateway_filter is None or gateway_filter == gateway_id:
                durations[gateway_id].append(milliseconds / 1000.0)
    for gateway_id, values in sorted(durations.items()):
        add(f"gateway.{gateway_id}.rpc_completed_window", len(values), "count", duration_ms)
        for label, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
            value = _percentile(values, quantile)
            if value is not None:
                add(f"gateway.{gateway_id}.rpc_duration_{label}", value, "second", duration_ms)
    return common.GetMetricsResponse(metrics=metrics)
