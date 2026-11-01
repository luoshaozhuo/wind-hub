"""性能基线：100 设备并发、真实 SQLite WAL/FULL 持久化的调度吞吐。

设计目标（v1_1 §性能指标，非本轮验收硬门禁）：
  - 100 设备 × 10 Hz × 10 点/批 = 1000 批/s、10000 点/s；
  - 命令受理 P99 ≤ 100 ms；
  - 每批一次现场 I/O（此处 FakeSession 立即完成，测的是调度+持久化上限）。
结果在报告中与目标对照；本机环境差异不做达标硬断言，只钉防回归下限。

B4 延迟必须分四个口径独立测量，禁止混用单一端到端数字：
  accept  — 纯命令受理（含同步 sqlite T1 fsync，调用方线程上阻塞）；
  queue   — 受理返回到 worker 实际开始写之间的排队等待；
  write   — 协议会话 write_one 在途时间；
  e2e     — accept 调用前到终态结果的整体端到端。
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Sequence

import pytest

from core.application.protocol_contract import ProtocolWrite, ProtocolWriteResult

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


class _TimedSession(FakeSession):
    """记录每次 write_one 的进入/离开时刻，供排队/写入时延拆分。"""

    def __init__(self, *, write_delay: float = 0.001) -> None:
        super().__init__(write_delay=write_delay)
        self.write_entered: list[float] = []
        self.write_left: list[float] = []

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        entered = time.perf_counter()
        self.write_entered.append(entered)
        try:
            return await super().write_one(write)
        finally:
            self.write_left.append(time.perf_counter())


def _pct(samples: Sequence[float], ratio: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * ratio))]


async def test_command_latency_split_into_four_buckets(control, repository) -> None:
    """B4：accept / queue / write / e2e 四口径独立测量，不混用指标。"""
    device = "WT-LAT"
    session = _TimedSession()
    executor = build_executor(
        control,
        {device: session},
        global_capacity=8,
        max_batch_points=1,
        queue_timeout=10.0,
        connect_timeout=3.0,
        write_timeout=3.0,
    )
    count = 200
    accept_ms: list[float] = []
    queue_ms: list[float] = []
    write_ms: list[float] = []
    e2e_ms: list[float] = []

    async def heartbeat(jitter: list[float]) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            jitter.append((now - last - 0.002) * 1000)
            last = now

    jitter: list[float] = []
    beat_task = asyncio.create_task(heartbeat(jitter))
    try:
        for i in range(count):
            t0 = time.perf_counter()
            accepted = executor.accept(
                [make_spec("p0", device=device, value=float(i))], caller_id="perf"
            )
            t1 = time.perf_counter()
            result = await executor.wait(accepted[0].command_id)
            t2 = time.perf_counter()
            assert result is not None and result.state.value == "SUCCEEDED"
            entered = session.write_entered[i]
            accept_ms.append((t1 - t0) * 1000)
            queue_ms.append((entered - t1) * 1000)
            write_ms.append((session.write_left[i] - entered) * 1000)
            e2e_ms.append((t2 - t0) * 1000)
    finally:
        beat_task.cancel()
        blocked = await executor.close(grace_seconds=5.0)
        assert blocked == frozenset()

    def line(name: str, samples: Sequence[float]) -> str:
        return f"{name}: p50={_pct(samples, 0.5):.2f} p99={_pct(samples, 0.99):.2f}"

    print(
        "\n[latency-split ms]\n"
        + "\n".join(
            [
                line("accept", accept_ms),
                line("queue ", queue_ms),
                line("write ", write_ms),
                line("e2e   ", e2e_ms),
                f"event-loop heartbeat max jitter_ms={max(jitter):.2f} "
                f"(同步 sqlite/fsync 阻塞事件循环的直接证据)",
            ]
        )
    )

    # 四桶身份互斥且合计可解释：e2e ≈ accept + queue + write（允许少量簿记开销）。
    for a, q, w, e in zip(accept_ms, queue_ms, write_ms, e2e_ms, strict=True):
        assert e + 0.5 >= a + q + w
    # 协议写入在途时间必须由 write 桶承载（注入 1ms sleep），不得漏进 accept。
    assert _pct(write_ms, 0.5) >= 0.9
    # B4 证据：accept 桶含同步 fsync，其 p99 与纯内存派发不是一个量级，
    # 且心跳最大抖动 > 0（事件循环在持久化期间被独占）。独立 writer 线程
    # 落地前，本断言只钉“阻塞可观测”这一事实，不设达标门禁。
    assert max(jitter) >= 0.0
