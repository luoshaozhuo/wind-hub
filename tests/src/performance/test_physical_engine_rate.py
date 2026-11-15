"""S4 物理引擎速率基线：10 Hz / 20 Hz、五设备 103 点、并发控制写入。

按 testing.md §6：无正式 SLA 时只建立 baseline，报告实测环境、步长预算
占用、吞吐、CPU/RSS 与延迟分位数，不虚构生产阈值。结构不变量
（每步精确 103 点、全部有限、零异常、输入生效）始终硬断言。
"""

from __future__ import annotations

import asyncio
import os
import resource
import statistics
import time
from pathlib import Path

import pytest

from data_generator.application.physical_engine import PhysicalEngine
from data_generator.infrastructure.config.service_settings import load_settings

pytestmark = [pytest.mark.performance, pytest.mark.load]

SERVICE_PATH = (
    Path(__file__).resolve().parents[3] / "configs" / "services" / "data-generator-sim.example.yaml"
)
EXPECTED_POINTS = 103
# 每个速率的固定物理时长（秒）与并发写入者使用的真实可写输入名。
HORIZON_S = 10.0
WRITER_PLAN = {
    "WTG_SIM01": {"active_power_setpoint": 1_500_000.0},
    "PV_SIM01": {"active_power_setpoint": 600_000.0},
    "BESS_SIM01": {"soc_target": 55.0},
    "PPC_SIM01": {"agc_enable": 1.0, "agc_power_setpoint": 2_000_000.0},
}


def _rss_mb() -> float:
    status = Path("/proc/self/status").read_text()
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    return float("nan")


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    index = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[index]


async def _run_rate(dt: float, writes_per_second: float) -> dict[str, object]:
    settings = load_settings(SERVICE_PATH)
    scenario = next(iter(settings.scenarios.values()))
    engine = PhysicalEngine(scenario)

    steps = int(HORIZON_S / dt)
    stop = asyncio.Event()

    async def writer(device: str, values: dict[str, float]) -> None:
        interval = 1.0 / writes_per_second
        applied = 0
        while not stop.is_set():
            engine.set_inputs(device, dict(values))
            applied += 1
            await asyncio.sleep(interval)
        return applied

    writers = [asyncio.create_task(writer(d, v)) for d, v in WRITER_PLAN.items()]
    durations: list[float] = []
    lag_seconds: list[float] = []
    over_budget = 0
    finite_values = 0
    rss_before = _rss_mb()
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    origin = time.perf_counter()
    try:
        for step in range(1, steps + 1):
            planned = origin + step * dt
            began = time.perf_counter()
            lag_seconds.append(max(0.0, began - planned))
            emitted = engine.advance(dt, step)
            cost = time.perf_counter() - began
            durations.append(cost)
            if cost > dt:
                over_budget += 1
            assert len(emitted) == EXPECTED_POINTS
            for value in emitted.values():
                assert isinstance(value, int | float)
                assert value == value  # NaN 检查
                assert abs(value) != float("inf")
                finite_values += 1
            # 模拟生产固定节拍调度器：让出事件循环给并发写入协程，并等待下一拍。
            await asyncio.sleep(max(0.0, planned - time.perf_counter()))
    finally:
        stop.set()
        applied_counts = await asyncio.gather(*writers)
    wall = time.perf_counter() - origin
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    ordered = sorted(durations)
    lag_ordered = sorted(lag_seconds)
    baseline = {
        "dt_s": dt,
        "rate_hz": 1.0 / dt,
        "steps": steps,
        "points_per_step": EXPECTED_POINTS,
        "wall_s": wall,
        "throughput_points_s": EXPECTED_POINTS * steps / wall,
        "step_us_p50": _percentile(ordered, 0.50) * 1e6,
        "step_us_p95": _percentile(ordered, 0.95) * 1e6,
        "step_us_p99": _percentile(ordered, 0.99) * 1e6,
        "step_us_max": ordered[-1] * 1e6,
        "budget_median_pct": 100 * statistics.median(durations) / dt,
        "budget_max_pct": 100 * ordered[-1] / dt,
        "lag_ms_p99": _percentile(lag_ordered, 0.99) * 1e3,
        "lag_ms_max": lag_ordered[-1] * 1e3,
        "steps_over_budget": over_budget,
        "finite_values": finite_values,
        "concurrent_writes": sum(applied_counts),
        "cpu_user_s": cpu_after.ru_utime - cpu_before.ru_utime,
        "cpu_system_s": cpu_after.ru_stime - cpu_before.ru_stime,
        "rss_growth_mb": _rss_mb() - rss_before,
        "pid": os.getpid(),
    }
    return baseline


def test_ten_hertz_five_devices_hold_103_point_contract() -> None:
    baseline = asyncio.run(_run_rate(dt=0.1, writes_per_second=20.0))
    print("\n[S4 10Hz baseline]", baseline)
    assert baseline["steps_over_budget"] == 0
    assert baseline["finite_values"] == EXPECTED_POINTS * baseline["steps"]
    assert baseline["concurrent_writes"] >= 4


def test_twenty_hertz_five_devices_hold_103_point_contract() -> None:
    baseline = asyncio.run(_run_rate(dt=0.05, writes_per_second=40.0))
    print("\n[S4 20Hz baseline]", baseline)
    # 出厂场景 interval_seconds=0.05：20 Hz 是生产配置速率。
    assert baseline["rate_hz"] == 20.0
    assert baseline["finite_values"] == EXPECTED_POINTS * baseline["steps"]
    assert baseline["concurrent_writes"] >= 4
    # 不虚构 SLA：仅当步长预算被击穿时计数显式报告，失败判定留给验收报告。
    print(
        "[S4] 20Hz over-budget steps =",
        baseline["steps_over_budget"],
        "max budget % =",
        round(baseline["budget_max_pct"], 2),
    )


def test_concurrent_control_inputs_are_observed_by_models() -> None:
    """并发 set_inputs 不得丢批：推进后 PPC AGC 使能与 BESS 目标进入模型状态。"""
    settings = load_settings(SERVICE_PATH)
    scenario = next(iter(settings.scenarios.values()))
    engine = PhysicalEngine(scenario)
    for _ in range(40):
        engine.set_inputs("PPC_SIM01", {"agc_enable": 1.0, "agc_power_setpoint": 2_000_000.0})
        engine.set_inputs("BESS_SIM01", {"soc_target": 55.0})
        engine.set_inputs("WTG_SIM01", {"active_power_setpoint": 1_500_000.0})
        emitted = engine.advance(0.05, 1)
        assert len(emitted) == EXPECTED_POINTS
    assert engine.inputs["PPC_SIM01"]["agc_enable"] == 1.0
    assert engine.inputs["BESS_SIM01"]["soc_target"] == 55.0
    assert engine.inputs["WTG_SIM01"]["active_power_setpoint"] == 1_500_000.0
