"""UC-06 组件级：计数真实性、窗口分位、空样本与序列限额。

直接构造 Runtime 计数器/观测值（无网络），指标导出走生产 snapshot()。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pytest

from data_generator.infrastructure.metrics import snapshot

pytestmark = [pytest.mark.component, pytest.mark.performance, pytest.mark.no_samples]


@dataclass
class FakeWriter:
    unsafe_gateways: frozenset[str] = frozenset()
    rpc_durations: deque[tuple[float, str, float]] = field(
        default_factory=lambda: deque(maxlen=4096)
    )


def _runtime(window_seconds: int = 60, max_series: int = 2000) -> Any:
    from types import SimpleNamespace

    runtime = SimpleNamespace()
    runtime.settings = SimpleNamespace(
        service={
            "metrics": {
                "window_seconds": window_seconds,
                "retain_seconds": 3600,
                "max_series": max_series,
            }
        },
    )
    runtime.counters = {"generated": 0, "batches": 0, "confirmed": 0}
    runtime.generations = {}
    runtime.writer = FakeWriter()
    runtime.observations = deque(maxlen=4096)
    runtime.gateway_counts = {"g1": {"SUCCESS": 0, "FAILED": 0, "UNKNOWN": 0}}
    runtime.replay_speed = None
    runtime.latest_replay_timestamp = None
    return runtime


def test_empty_window_emits_no_percentile_series_not_zero_p99() -> None:
    response = snapshot(_runtime(), "", 0)
    names = {metric.name for metric in response.metrics}
    assert "data_generator.generated_total" in names
    assert not any("p50" in name or "p99" in name for name in names)


def test_nearest_rank_percentiles_match_real_samples() -> None:
    import time

    runtime = _runtime()
    now = time.monotonic()
    for value in range(1, 101):  # 1..100
        runtime.observations.append((now, "simulation_duration_seconds", value / 1000.0))
    response = snapshot(runtime, "", 0)
    values = {metric.name: metric.value for metric in response.metrics}
    # nearest-rank: p50=0.050, p95=0.095, p99=0.099
    assert values["data_generator.simulation_duration_seconds_p50"] == pytest.approx(0.050)
    assert values["data_generator.simulation_duration_seconds_p95"] == pytest.approx(0.095)
    assert values["data_generator.simulation_duration_seconds_p99"] == pytest.approx(0.099)


def test_window_ms_filters_stale_observations() -> None:
    import time

    runtime = _runtime()
    now = time.monotonic()
    runtime.observations.append((now - 10.0, "simulation_duration_seconds", 1.0))
    runtime.observations.append((now, "simulation_duration_seconds", 2.0))
    response = snapshot(runtime, "", 1000)
    values = {metric.name: metric.value for metric in response.metrics}
    assert values["data_generator.simulation_duration_seconds_p99"] == pytest.approx(2.0)


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError, match="invalid metrics window"):
        snapshot(_runtime(), "", -1)
    with pytest.raises(ValueError, match="invalid metrics window"):
        snapshot(_runtime(), "", 3_600_001)


def test_scope_filters_gateway_dimensions() -> None:
    runtime = _runtime()
    runtime.gateway_counts = {
        "g1": {"SUCCESS": 5, "FAILED": 1, "UNKNOWN": 0},
        "g2": {"SUCCESS": 9, "FAILED": 0, "UNKNOWN": 0},
    }
    response = snapshot(runtime, "gateway:g2", 0)
    names = {metric.name for metric in response.metrics}
    assert any("gateway.g2.success_total" in name for name in names)
    assert not any("gateway.g1." in name for name in names)
    with pytest.raises(ValueError, match="scope"):
        snapshot(runtime, "galaxy:g1", 0)
    with pytest.raises(ValueError, match="nonempty"):
        snapshot(runtime, "gateway:", 0)


def test_series_capacity_silently_bounds_output() -> None:
    runtime = _runtime(max_series=5)
    runtime.counters = {f"counter_{i}": i for i in range(20)}
    response = snapshot(runtime, "", 0)
    # 当前实现：达到 max_series 后静默丢弃（无溢出计数）——记录既有行为。
    assert len(response.metrics) == 5
