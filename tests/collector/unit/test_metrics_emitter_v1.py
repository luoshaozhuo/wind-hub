"""Fixed-window metrics publication envelope and capacity tests."""

from __future__ import annotations

import json
import time

import pytest

from collector.application.telemetry import CollectorTelemetry
from collector.infrastructure.metrics_emitter import CollectorMetricsEmitter

pytestmark = pytest.mark.unit


def _emitter(telemetry: CollectorTelemetry, capacity: int = 2) -> CollectorMetricsEmitter:
    return CollectorMetricsEmitter(
        telemetry,
        servers=["nats://127.0.0.1:4222"],
        site_id="farm",
        collector_id="collector",
        interval_seconds=60,
        max_staleness_seconds=300,
        capacity=capacity,
    )


def test_emits_only_observed_utc_fixed_window_counts() -> None:
    telemetry = CollectorTelemetry(windows_seconds=(60, 300))
    emitter = _emitter(telemetry)
    current = int(time.time())
    end = (current // 60 + 1) * 60
    emitter._capture(end)
    assert not emitter._pending
    telemetry.increment("collector.points.collected", 4)
    telemetry.increment("collector.points.bad", 1)
    emitter._capture(end)
    assert len(emitter._pending) == 1
    payload = json.loads(emitter._pending[0].payload)
    assert payload["schema_version"] == 1
    assert payload["collector_id"] == "collector"
    assert payload["boot_id"]
    assert payload["window_id"].startswith(payload["boot_id"])
    assert payload["counters"] == {
        "collector.points.collected": 4,
        "collector.points.bad": 1,
    }
    assert payload["window_start"] < payload["window_end"]


def test_pending_fixed_window_capacity_drops_oldest() -> None:
    telemetry = CollectorTelemetry()
    emitter = _emitter(telemetry, capacity=1)
    telemetry.increment("samples", 1)
    end = (int(time.time()) // 60 + 1) * 60
    emitter._capture(end)
    emitter._capture(end)
    assert len(emitter._pending) == 1
    assert emitter.dropped == 1


def test_invalid_metrics_emitter_limits() -> None:
    with pytest.raises(ValueError):
        CollectorMetricsEmitter(
            CollectorTelemetry(),
            servers=["nats://127.0.0.1:4222"],
            site_id="farm",
            collector_id="collector",
            interval_seconds=0,
            max_staleness_seconds=1,
        )
