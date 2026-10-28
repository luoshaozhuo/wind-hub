"""统一只读测速统计；不控制通信资源生命周期。"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Awaitable, Callable

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus


async def benchmark(
    target: str,
    operation: Callable[[], Awaitable[int]],
    *,
    samples: int = 10,
    interval: float = 0.2,
) -> DiagnosticResult:
    """顺序执行有界操作，返回请求和测点吞吐；一次失败不自动重试。"""
    if not 1 <= samples <= 200 or not 0 <= interval <= 5:
        raise ValueError("samples must be 1..200 and interval 0..5")
    import asyncio

    durations: list[float] = []
    success_points = 0
    failures = 0
    started = time.perf_counter()
    for index in range(samples):
        begin = time.perf_counter()
        try:
            points = await operation()
            success_points += points
            durations.append((time.perf_counter() - begin) * 1000)
        except Exception:
            failures += 1
        if index + 1 < samples and interval:
            await asyncio.sleep(interval)
    elapsed = max(time.perf_counter() - started, 1e-9)
    ordered = sorted(durations)

    def percentile(percentage: float) -> float:
        if not ordered:
            return math.nan
        position = (len(ordered) - 1) * percentage
        lo = math.floor(position)
        hi = math.ceil(position)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)

    measurements: dict[str, int | float | str] = {
        "requests": samples,
        "successes": len(durations),
        "failures": failures,
        "points": success_points,
        "duration_s": round(elapsed, 3),
        "requests_per_s": round(len(durations) / elapsed, 3),
        "points_per_s": round(success_points / elapsed, 3),
    }
    if ordered:
        measurements.update(
            {
                "mean_ms": round(statistics.mean(ordered), 3),
                "p50_ms": round(percentile(0.5), 3),
                "p95_ms": round(percentile(0.95), 3),
                "p99_ms": round(percentile(0.99), 3),
            }
        )
    status = (
        DiagnosticStatus.FAIL
        if not ordered
        else DiagnosticStatus.WARNING
        if failures
        else DiagnosticStatus.PASS
    )
    return DiagnosticResult(
        category="benchmark",
        target=target,
        status=status,
        findings=(
            DiagnosticFinding(
                code="BENCHMARK_COMPLETE",
                status=status,
                message="Read-only sequential benchmark; no write operations performed",
            ),
        ),
        measurements=measurements,
    )
