"""性能基线：100 设备并发、真实 SQLite WAL/FULL 持久化的调度吞吐。

设计目标（v1_1 §性能指标，非本轮验收硬门禁）：
  - 100 设备 × 10 Hz × 10 点/批 = 1000 批/s、10000 点/s；
  - 命令受理 P99 ≤ 100 ms；
  - 每批一次现场 I/O（此处 FakeSession 立即完成，测的是调度+持久化上限）。
结果在报告中与目标对照；本机环境差异不做达标硬断言，只钉防回归下限。
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from ..component.conftest import FakeSession, build_executor, make_spec

pytestmark = [pytest.mark.performance, pytest.mark.slow]

DEVICE_COUNT = 100
COMMANDS_PER_DEVICE = 10
BATCH_POINTS = 10


async def test_dispatch_throughput_100_devices_real_sqlite(control, repository) -> None:
    sessions = {f"WT{i:03d}": FakeSession() for i in range(DEVICE_COUNT)}
    executor = build_executor(
        control,
        sessions,
        global_capacity=100,
        max_queued_batches_per_device=20,
        max_queued_batches_global=2000,
        max_batch_points=BATCH_POINTS,
        queue_timeout=10.0,
        connect_timeout=3.0,
        write_timeout=3.0,
    )

    async def run_device(device: str) -> list[float]:
        latencies: list[float] = []
        for _ in range(COMMANDS_PER_DEVICE):
            specs = tuple(
                make_spec(f"p{point}", device=device, value=float(point))
                for point in range(BATCH_POINTS)
            )
            # 无 WRITE_MANY：同请求 10 点顺序走 write_one，等于 10 次现场写。
            t0 = time.perf_counter()
            accepted = executor.accept(list(specs), caller_id="perf")
            await asyncio.gather(*(executor.wait(c.command_id) for c in accepted))
            latencies.append((time.perf_counter() - t0) * 1000)
        return latencies

    started = time.perf_counter()
    try:
        per_device = await asyncio.gather(*(run_device(f"WT{i:03d}") for i in range(DEVICE_COUNT)))
    finally:
        blocked = await executor.close(grace_seconds=10.0)
        assert blocked == frozenset()
    elapsed = time.perf_counter() - started

    total_commands = DEVICE_COUNT * COMMANDS_PER_DEVICE * BATCH_POINTS
    total_batches = DEVICE_COUNT * COMMANDS_PER_DEVICE
    all_latency = [value for values in per_device for value in values]
    all_latency.sort()
    p50 = all_latency[len(all_latency) // 2]
    p99 = all_latency[min(len(all_latency) - 1, int(len(all_latency) * 0.99))]
    points_per_second = total_commands / elapsed
    batches_per_second = total_batches / elapsed

    print(
        "\n[perf] "
        f"batches={total_batches} elapsed={elapsed:.2f}s "
        f"batches/s={batches_per_second:.0f} points/s={points_per_second:.0f} "
        f"latency_ms p50={p50:.1f} p99={p99:.1f} "
        f"jitter_ms={statistics.pstdev(all_latency):.1f}"
    )

    # 全部确证成功，无一过期/UNKNOWN。
    assert executor.pending_commands() == 0
    assert sum(len(s.one_calls) for s in sessions.values()) == total_commands
    terminal = dict(repository.metrics().terminal_counts)
    assert terminal.get("SUCCEEDED") == total_commands
    assert not (set(terminal) - {"SUCCEEDED"})

    # 防回归下限（设计目标 1000 批/s；真实 fsync 环境下只钉宽松地板）。
    assert batches_per_second >= 100
    assert p99 <= 500.0
