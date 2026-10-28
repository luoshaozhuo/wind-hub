"""统一只读测速统计 benchmark() 的行为契约。

覆盖：全成功/部分失败/全失败、p50/p95/p99 统计、空样本、参数边界、
NaN/Inf、返回点数非非负整数、间隔等待、失败后不重试、取消传播。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterator

import pytest

from core.application.diagnostic import DiagnosticStatus
from core.infrastructure.diagnostic.benchmark import benchmark


def _durations() -> list[float]:
    return [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]


async def test_all_success_reports_pass_and_counts() -> None:
    durations = _durations()

    async def operation() -> int:
        await asyncio.sleep(0)
        durations.pop(0)
        return 2

    result = await benchmark("t", operation, samples=10, interval=0)

    assert result.status is DiagnosticStatus.PASS
    assert result.target == "t"
    m = result.measurements
    assert m["requests"] == 10
    assert m["successes"] == 10
    assert m["failures"] == 0
    assert m["points"] == 20
    assert m["requests_per_s"] > 0
    assert m["points_per_s"] > 0
    # 顺序采样下 p50 为中位样本，p99 应接近最慢样本且分位数单调。
    assert 0 < m["p50_ms"] <= m["p95_ms"] <= m["p99_ms"]
    assert result.findings[0].code == "BENCHMARK_COMPLETE"


async def test_partial_failure_is_warning_and_keeps_success_stats() -> None:
    calls = {"n": 0}

    async def operation() -> int:
        calls["n"] += 1
        if calls["n"] in {2, 5}:
            raise RuntimeError("device timeout")
        return 1

    result = await benchmark("t", operation, samples=5, interval=0)

    assert result.status is DiagnosticStatus.WARNING
    assert result.measurements["failures"] == 2
    assert result.measurements["successes"] == 3
    assert result.measurements["points"] == 3
    # 一次失败不自动重试：总尝试次数恰好等于样本数。
    assert calls["n"] == 5
    assert "p50_ms" in result.measurements


async def test_all_failures_is_fail_without_latency_stats() -> None:
    async def operation() -> int:
        raise ConnectionError("unreachable")

    result = await benchmark("t", operation, samples=4, interval=0)

    assert result.status is DiagnosticStatus.FAIL
    m = result.measurements
    assert m["requests"] == 4
    assert m["successes"] == 0
    assert m["failures"] == 4
    assert m["points"] == 0
    assert m["requests_per_s"] == 0
    assert m["points_per_s"] == 0
    assert "p50_ms" not in m
    assert "p99_ms" not in m


async def test_single_sample_needs_no_interval() -> None:
    async def operation() -> int:
        return 7

    result = await benchmark("t", operation, samples=1)

    assert result.status is DiagnosticStatus.PASS
    assert result.measurements["requests"] == 1
    assert result.measurements["points"] == 7


@pytest.mark.parametrize(
    "samples",
    [0, -1, 201],
)
async def test_samples_out_of_range_rejected(samples: int) -> None:
    async def operation() -> int:
        return 1

    with pytest.raises(ValueError, match="samples"):
        await benchmark("t", operation, samples=samples)


@pytest.mark.parametrize("interval", [-0.01, 5.01, math.nan, math.inf, -math.inf])
async def test_interval_must_be_finite_and_in_range(interval: float) -> None:
    async def operation() -> int:
        return 1

    with pytest.raises(ValueError, match="interval"):
        await benchmark("t", operation, samples=2, interval=interval)


async def test_zero_points_is_valid_but_negative_is_failure() -> None:
    sequence: Iterator[int] = iter([0, -1, 0])

    async def operation() -> int:
        return next(sequence)

    result = await benchmark("t", operation, samples=3, interval=0)

    # 0 是合法的「本次无测点」；负数必须计为失败。
    assert result.status is DiagnosticStatus.WARNING
    assert result.measurements["points"] == 0
    assert result.measurements["successes"] == 2
    assert result.measurements["failures"] == 1


async def test_bool_point_count_is_rejected_not_counted_as_int() -> None:
    async def operation() -> int:
        return True  # type: ignore[return-value]

    result = await benchmark("t", operation, samples=1, interval=0)

    assert result.status is DiagnosticStatus.FAIL
    assert result.measurements["successes"] == 0


async def test_interval_wait_is_actually_observed() -> None:
    async def operation() -> int:
        return 1

    started = asyncio.get_running_loop().time()
    await benchmark("t", operation, samples=3, interval=0.05)
    elapsed = asyncio.get_running_loop().time() - started

    # 3 个样本之间应发生 2 次 interval 等待（允许调度抖动）。
    assert elapsed >= 0.09


async def test_cancellation_propagates_and_stops_further_samples() -> None:
    attempts = 0

    async def operation() -> int:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.05)
        return 1

    task = asyncio.create_task(benchmark("t", operation, samples=100, interval=0))
    await asyncio.sleep(0.12)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts < 100
