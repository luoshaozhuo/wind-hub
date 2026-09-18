"""Unit tests for ``tests/perf/collector.py`` — 性能指标采集。

/proc 读取函数（模块级 seam）全部 monkeypatch，不读真实 /proc。
"""

from __future__ import annotations

import pytest

from tests.perf import collector as collector_mod
from tests.perf.collector import MetricsCollector, _percentile


class _Stats:
    """collector.SchedulerStats 协议的桩实现。"""

    def __init__(self, collected: int, routed: int, dropped: int) -> None:
        self.points_collected = collected
        self.points_routed = routed
        self.points_dropped = dropped


@pytest.fixture
def fake_proc(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """可控的 /proc 读数：cpu 秒数、内存 MiB、FD 数。"""
    state = {"cpu_s": 10.0, "memory_mb": 100.0, "fd_count": 42}
    monkeypatch.setattr(collector_mod, "_read_cpu_seconds", lambda: state["cpu_s"])
    monkeypatch.setattr(collector_mod, "_read_memory_mb", lambda: state["memory_mb"])
    monkeypatch.setattr(collector_mod, "_read_fd_count", lambda: int(state["fd_count"]))
    return state


# ---------------------------------------------------------------------------
# 百分位
# ---------------------------------------------------------------------------


def test_percentile_empty_is_zero() -> None:
    assert _percentile([], 50) == 0.0
    assert _percentile([], 99) == 0.0


def test_percentile_nearest_rank() -> None:
    values = sorted([10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0])
    assert _percentile(values, 50) == 50.0
    assert _percentile(values, 95) == 100.0
    assert _percentile(values, 99) == 100.0
    assert _percentile(values, 10) == 10.0


# ---------------------------------------------------------------------------
# 延迟与重连记录 → build_metrics
# ---------------------------------------------------------------------------


def test_build_metrics_aggregates_latency_and_reconnect() -> None:
    c = MetricsCollector()
    for ms in [1.0, 2.0, 3.0, 4.0, 100.0]:
        c.record_latency(ms)
    c.record_reconnect(250.0)
    c.record_reconnect(900.0)

    m = c.build_metrics("modbus", "ideal", _Stats(500, 500, 0), duration_s=10.0)

    assert m.protocol == "modbus"
    assert m.scenario == "ideal"
    assert m.points_collected == 500
    assert m.points_routed == 500
    assert m.points_dropped == 0
    assert m.throughput_pps == 50.0
    assert m.latency_p50_ms == 3.0
    assert m.latency_max_ms == 100.0
    assert m.reconnect_count == 2
    assert m.reconnect_time_ms == 900.0  # 单次最长恢复


def test_build_metrics_without_samples() -> None:
    m = MetricsCollector().build_metrics("ads", "ideal", _Stats(0, 0, 0), duration_s=60.0)
    assert m.throughput_pps == 0.0
    assert m.latency_p50_ms == 0.0
    assert m.reconnect_count == 0


def test_reset_measurement_clears_latency_and_reconnect() -> None:
    c = MetricsCollector()
    c.record_latency(5.0)
    c.record_reconnect(100.0)
    c.reset_measurement()
    c.record_latency(7.0)

    m = c.build_metrics("iec104", "ideal", _Stats(1, 1, 0), duration_s=1.0)
    assert m.latency_p50_ms == 7.0
    assert m.latency_max_ms == 7.0
    assert m.reconnect_count == 0


# ---------------------------------------------------------------------------
# 资源采样
# ---------------------------------------------------------------------------


async def test_resource_sampling_peaks_and_cpu(fake_proc: dict[str, float]) -> None:
    c = MetricsCollector(sample_interval_s=0.01)
    await c.start()
    # 模拟运行期资源爬升：CPU 时间前进 0.5s、内存涨到 200、FD 涨到 88
    fake_proc["cpu_s"] += 0.5
    fake_proc["memory_mb"] = 200.0
    fake_proc["fd_count"] = 88
    await c.stop()

    m = c.build_metrics("modbus", "ideal", _Stats(0, 0, 0), duration_s=1.0)
    assert m.memory_mb == 200.0  # 峰值
    assert m.fd_count == 88  # 峰值
    assert m.cpu_percent > 0.0  # 0.5s CPU / 墙钟 > 0


async def test_reset_measurement_rebases_resources(fake_proc: dict[str, float]) -> None:
    c = MetricsCollector(sample_interval_s=0.01)
    await c.start()
    fake_proc["memory_mb"] = 500.0  # 预热期峰值样本（应被 reset 丢弃）
    fake_proc["memory_mb"] = 100.0  # 预热结束后回落（reset 基线读到的值）
    c.reset_measurement()
    fake_proc["memory_mb"] = 120.0
    await c.stop()

    m = c.build_metrics("modbus", "ideal", _Stats(0, 0, 0), duration_s=1.0)
    # 峰值只统计 reset 之后的样本：100（基线）与 120，预热期的 500 已被丢弃
    assert m.memory_mb == 120.0


async def test_start_stop_is_idempotent(fake_proc: dict[str, float]) -> None:
    c = MetricsCollector(sample_interval_s=0.01)
    await c.start()
    await c.stop()
    await c.stop()  # 重复 stop 不报错
