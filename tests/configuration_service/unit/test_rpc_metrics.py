"""Bounded RPC metric sampling semantics."""

from __future__ import annotations

import pytest

from configuration_service.application.metrics import RpcMetrics

pytestmark = pytest.mark.unit


def test_metrics_empty_window_and_latency_percentiles(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("configuration_service.application.metrics.time.monotonic", lambda: now[0])
    metrics = RpcMetrics(max_samples=3, retention_seconds=1)
    empty = metrics.snapshot(window_ms=100)
    assert empty.request_count == empty.failed_count == 0
    assert empty.average_latency_ms == empty.p95_latency_ms == 0.0
    for index in range(1, 5):
        metrics.observe(duration_ms=float(index), success=index != 2)
    sample = metrics.snapshot(window_ms=1000)
    assert sample.request_count == 3
    assert sample.failed_count == 1
    assert sample.average_latency_ms == 3.0
    assert sample.p95_latency_ms == 4.0
    now[0] += 2
    assert metrics.snapshot(window_ms=1000).request_count == 0


def test_metrics_reject_invalid_samples() -> None:
    metrics = RpcMetrics()
    with pytest.raises(ValueError):
        metrics.observe(duration_ms=-1, success=True)
    with pytest.raises(ValueError):
        metrics.observe(duration_ms=float("nan"), success=True)
