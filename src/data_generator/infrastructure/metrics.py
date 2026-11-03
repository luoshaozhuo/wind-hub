"""Bounded read-only metric snapshot and nearest-rank latency percentiles."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from contracts import common_pb2 as common

if TYPE_CHECKING:
    from data_generator.application.runtime import Runtime


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[rank]


def snapshot(runtime: Runtime, scope: str, window_ms: int) -> common.GetMetricsResponse:
    """Do not synthesize percentiles without completed RPC observations."""
    configured = runtime.settings.service.get("metrics", {})
    default_ms = int(float(configured.get("window_seconds", 60)) * 1000)
    maximum_ms = int(float(configured.get("retain_seconds", 3600)) * 1000)
    selected_ms = window_ms or default_ms
    if not 0 < selected_ms <= maximum_ms:
        raise ValueError("invalid metrics window")
    if scope not in ("", "service") and not scope.startswith("gateway:"):
        raise ValueError("unsupported metrics scope")
    gateway_filter = scope.removeprefix("gateway:") if scope.startswith("gateway:") else None
    now = time.monotonic()
    observed = [
        (gateway_id, duration)
        for completed_at, gateway_id, duration in runtime.writer.rpc_durations
        if (now - completed_at) * 1000 <= selected_ms
        and (gateway_filter is None or gateway_filter == gateway_id)
    ]
    from datetime import datetime, timezone

    from google.protobuf.timestamp_pb2 import Timestamp

    sampled_at = Timestamp()
    sampled_at.FromDatetime(datetime.now(timezone.utc))
    metrics: list[common.Metric] = []

    def emit(name: str, value: float, unit: str, window: int = 0) -> None:
        metrics.append(common.Metric(
            name=name, value=value, unit=unit,
            window_ms=window, sampled_at=sampled_at,
        ))

    if gateway_filter is None:
        for key, count in sorted(runtime.counters.items()):
            emit(f"data_generator.{key}_total", float(count), "count")
        emit("data_generator.active_generations",
             float(sum(state.actual_state in ("STARTING", "RUNNING", "STOPPING")
                       for state in runtime.generations.values())), "count")

    grouped: dict[str, list[float]] = defaultdict(list)
    for gateway_id, duration in observed:
        grouped[gateway_id].append(duration)
    if gateway_filter is not None and not grouped:
        grouped[gateway_filter] = []
    for gateway_id, samples in sorted(grouped.items()):
        emit(f"gateway.{gateway_id}.rpc_completed", float(len(samples)),
             "count", selected_ms)
        for label, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
            value = _percentile(samples, fraction)
            if value is not None:
                emit(f"gateway.{gateway_id}.rpc_duration_{label}",
                     value, "millisecond", selected_ms)
    return common.GetMetricsResponse(metrics=metrics)
