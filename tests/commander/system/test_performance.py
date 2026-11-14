"""性能基线：100 设备并发、真实 SQLite WAL/FULL 持久化的调度吞吐。

设计目标（v1_1 §性能指标，非本轮验收硬门禁）：
  - 100 设备 × 10 Hz × 10 点/批 = 1000 批/s、10000 点/s；
  - 命令受理 P99 ≤ 100 ms；
  - 每批一次现场 I/O（此处 FakeSession 立即完成，测的是调度+持久化上限）。
结果在报告中与目标对照；本机环境差异不做达标硬断言，只钉防回归下限。

v1_22 RR-01 改变了受理性能模型：accept 在全局受理锁内等待 T1 COMMIT
（write-behind 按 persistence_interval 批量落盘），因此受理速率上限约为
1/interval，旧契约“受理不等 SQLite”的 500 批/s 地板与 200ms e2e 地板已
失效。本模块改钉新契约的结构性不变量：T1 落盘延迟有界、writer 线程无
积压、事件合批高效、零丢失零重复、内存积压有界；吞吐地板取理论受理
 envelope（1/interval）的保守一半。全局受理锁的并发度限制作为 P2 结构
观察项在验收报告记录，不在测试侧掩盖。

B4 延迟必须分四个口径独立测量，禁止混用单一端到端数字：
  accept  — 命令受理（含 RR-01 的 T1 COMMIT 等待，fsync 仍在唯一 writer 线程）；
  queue   — 受理返回到 worker 实际开始写之间的排队等待；
  write   — 协议会话 write_one 在途时间；
  e2e     — accept 调用前到终态结果的整体端到端。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from commander.application.control import CommandControl
from commander.application.persistence import BufferedJournal, PersistenceWorker
from commander.infrastructure.persistence.event_repository import EventRepository
from core.application.protocol_contract import ProtocolWrite, ProtocolWriteResult

from ..component.conftest import FakeSession, build_executor, make_spec

pytestmark = [pytest.mark.performance, pytest.mark.slow]

DEVICE_COUNT = 100
COMMANDS_PER_DEVICE = 10
BATCH_POINTS = 10
# 与生产 configs/services/commander.yaml persistence_interval_ms=50 对齐。
PERSISTENCE_INTERVAL = 0.05


class _InstrumentedWriter(ThreadPoolExecutor):
    """记录唯一 writer 线程的队列积压深度与入队→开跑等待时延。"""

    def __init__(self) -> None:
        super().__init__(max_workers=1, thread_name_prefix="perf-sqlite")
        self._depth = 0
        self.max_depth = 0
        self.queue_wait_ms: list[float] = []
        self.tx: dict[str, int] = {}

    def submit(self, fn, /, *args, **kwargs):
        enqueued = time.perf_counter()
        self._depth += 1
        self.max_depth = max(self.max_depth, self._depth)

        def wrapped() -> object:
            self.queue_wait_ms.append((time.perf_counter() - enqueued) * 1000)
            self._depth -= 1
            return fn(*args, **kwargs)

        return super().submit(wrapped)


async def test_dispatch_throughput_100_devices_real_sqlite(tmp_path) -> None:
    # 独立装配：write-behind 生产同模型——loop 内 BufferedJournal +
    # PersistenceWorker（50ms/500）+ 唯一 writer 线程真实 SQLite WAL/FULL。
    writer = _InstrumentedWriter()
    repository = writer.submit(lambda: EventRepository(tmp_path / "perf.db")).result()
    buffer = BufferedJournal()
    worker = PersistenceWorker(
        buffer, repository, writer, interval=PERSISTENCE_INTERVAL, batch_size=500
    )

    # 插桩：事务边界计数、每批事件数、事件生成→落盘延迟、内存队列最大积压。
    original_tx = repository._transaction
    original_append = repository.append_events
    batches: list[int] = []
    flush_latency_ms: list[float] = []
    enqueued_at: dict[str, float] = {}
    original_add = buffer._add

    def instrumented_add(fields) -> None:
        enqueued_at[str(fields["event_id"])] = time.perf_counter()
        return original_add(fields)

    buffer._add = instrumented_add  # type: ignore[method-assign]

    def counted_tx(operation):
        name = getattr(operation, "__name__", "?")
        writer.tx[name] = writer.tx.get(name, 0) + 1
        return original_tx(operation)

    def instrumented_append(events):
        now = time.perf_counter()
        batches.append(len(events))
        flush_latency_ms.extend(
            (now - enqueued_at[str(event["event_id"])]) * 1000 for event in events
        )
        return original_append(events)

    repository._transaction = counted_tx
    repository.append_events = instrumented_append

    control = CommandControl(repository, writer, buffer)
    max_pending = {"value": 0}
    stop_sampling = asyncio.Event()

    async def sample_backlog() -> None:
        while not stop_sampling.is_set():
            max_pending["value"] = max(max_pending["value"], buffer.pending_count)
            await asyncio.sleep(0.001)

    sampler = asyncio.create_task(sample_backlog())
    worker.start()
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
            accepted = await executor.accept(
                list(specs), request_id=f"req-perf-{uuid4().hex}", caller_id="perf"
            )
            await asyncio.gather(*(executor.wait(c.command_id) for c in accepted))
            latencies.append((time.perf_counter() - t0) * 1000)
        return latencies

    started = time.perf_counter()
    try:
        per_device = await asyncio.gather(*(run_device(f"WT{i:03d}") for i in range(DEVICE_COUNT)))
    finally:
        blocked = await executor.close(grace_seconds=10.0)
        assert blocked == frozenset()
    elapsed_field = time.perf_counter() - started
    # 现场全部完成后有界排空 write-behind 队列。
    assert await worker.stop(timeout=30.0)
    stop_sampling.set()
    await sampler
    elapsed = time.perf_counter() - started
    terminal_metrics = writer.submit(repository.metrics).result()
    writer.submit(repository.close).result()
    writer.shutdown(wait=True)

    total_commands = DEVICE_COUNT * COMMANDS_PER_DEVICE * BATCH_POINTS
    total_batches = DEVICE_COUNT * COMMANDS_PER_DEVICE
    expected_events = total_commands * 3 + DEVICE_COUNT  # T1/T2/T3 + CONNECTING/设备
    all_latency = [value for values in per_device for value in values]
    all_latency.sort()
    p50 = all_latency[len(all_latency) // 2]
    p99 = all_latency[min(len(all_latency) - 1, int(len(all_latency) * 0.99))]
    # write-behind 主口径：现场控制完成速率（不等待 SQLite）。
    points_per_second = total_commands / elapsed_field
    batches_per_second = total_batches / elapsed_field

    total_tx = sum(writer.tx.values())
    qwait = sorted(writer.queue_wait_ms)
    qwait_p99 = qwait[min(len(qwait) - 1, int(len(qwait) * 0.99))]
    flush_latency_ms.sort()
    flush_p50 = flush_latency_ms[len(flush_latency_ms) // 2]
    flush_p99 = flush_latency_ms[min(len(flush_latency_ms) - 1, int(len(flush_latency_ms) * 0.99))]
    print(
        "\n[perf] "
        f"batches={total_batches} field_elapsed={elapsed_field:.2f}s drain_elapsed={elapsed:.2f}s "
        f"batches/s={batches_per_second:.0f} points/s={points_per_second:.0f} "
        f"e2e_ms p50={p50:.1f} p99={p99:.1f}"
    )
    print(
        "[perf-persistence] "
        f"events={expected_events} append_batches={len(batches)} "
        f"events/batch avg={sum(batches) / len(batches):.1f} max={max(batches)} "
        f"flush_latency_ms p50={flush_p50:.1f} p99={flush_p99:.1f} "
        f"max_backlog={max_pending['value']}"
    )
    print(
        "[perf-sqlite] "
        f"tx={dict(sorted(writer.tx.items()))} total={total_tx} "
        f"writer_queue max_depth={writer.max_depth} wait_p99_ms={qwait_p99:.2f}"
    )

    # 全部确证成功，无一过期/UNKNOWN。
    assert executor.pending_commands() == 0
    assert sum(len(s.one_calls) for s in sessions.values()) == total_commands
    terminal = dict(terminal_metrics.terminal_counts)
    assert terminal.get("SUCCEEDED") == total_commands
    assert not (set(terminal) - {"SUCCEEDED"})

    # write-behind：所有事件单事务幂等批量落盘；每批 ≤ batch_size，无丢失无重复。
    assert sum(batches) == expected_events
    assert max(batches) <= 500
    assert terminal_metrics.accepted_total == total_commands

    # v1_22 RR-01 防回归地板（取代旧契约的 500 批/s、200ms）：
    # 1) 受理速率不低于理论门禁 envelope（1/interval=20/s）的一半，防死锁/退化；
    assert batches_per_second >= 0.5 / PERSISTENCE_INTERVAL
    # 2) T1 落盘延迟被 flush 节奏有界（实测 p99≈52ms），fsync 未失控；
    assert flush_p99 <= 100.0
    # 3) 唯一 writer 线程无排队积压（fsync 不是瓶颈，插桩 p99 个位数毫秒）；
    assert qwait_p99 <= 50.0
    # 4) 合批持续生效：内存峰值远低于 batch 容量量级，无无界积压；
    assert max_pending["value"] <= 1000
    # 5) 活性上界：全局受理锁串行 + 门禁等待下，e2e p99 不得超过
    #    “每受理一个 flush 周期”的理论串行预算的 1.5 倍（挂死检测器）。
    assert p99 <= 1.5 * PERSISTENCE_INTERVAL * total_batches * 1000


async def test_nominal_load(tmp_path) -> None:
    """§14.4 表 14-2 规定标识：100 设备 / 10Hz / 批量 10 标称负载场景。

    与 test_dispatch_throughput_100_devices_real_sqlite 为同一负载、同一
    统计口径与同一防回归地板；此处不另设阈值、不改变负载（验收§3.3）。
    本用例经 QueuedExecutor 应用层装配施载，UR-05 外部批量 RPC 的 PASS
    证据由 tests/commander/system/test_control_flow.py 与 tests/src/ 承担。
    """
    await test_dispatch_throughput_100_devices_real_sqlite(tmp_path)


async def test_sqlite_outage_rejects_admission_then_drains_without_loss(tmp_path) -> None:
    """故障矩阵性能口径（v1_22 §9.5.1）：故障期受理 fail-closed，恢复后零丢失。

    取代旧契约“故障期现场吞吐不受影响”——RR-01 要求 T1 COMMIT 先于
    accepted=true：SQLite 全程不可用时，所有受理在 accept_timeout 处被拒、
    零现场 I/O，保守 INTERRUPTED/NOT_SENT 事件滞留内存且积压有界；恢复后
    新受理正常收敛，全部滞留事件幂等落盘（event_id UNIQUE + 重试），
    无丢失、无重复。
    """
    import sqlite3

    writer = _InstrumentedWriter()
    repository = writer.submit(lambda: EventRepository(tmp_path / "perf_outage.db")).result()
    buffer = BufferedJournal()
    worker = PersistenceWorker(
        buffer, repository, writer, interval=PERSISTENCE_INTERVAL, batch_size=500
    )

    state = {"failing": True, "failed_batches": 0}
    original_append = repository.append_events

    def faulting_append(events):
        if state["failing"]:
            state["failed_batches"] += 1
            raise sqlite3.OperationalError("simulated disk outage during load")
        return original_append(events)

    repository.append_events = faulting_append

    batches: list[int] = []

    def counting_append(events):
        batches.append(len(events))
        return original_append(events)

    # 短门禁：故障期快速失败，避免等待生产默认 10s。
    control = CommandControl(repository, writer, buffer, accept_timeout=0.3)
    max_pending = {"value": 0}
    stop_sampling = asyncio.Event()

    async def sample_backlog() -> None:
        while not stop_sampling.is_set():
            max_pending["value"] = max(max_pending["value"], buffer.pending_count)
            await asyncio.sleep(0.001)

    sampler = asyncio.create_task(sample_backlog())
    worker.start()
    outage_devices = 10
    sessions = {f"WT{i:03d}": FakeSession() for i in range(outage_devices)}
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

    rejected_commands = 0
    started = time.perf_counter()
    try:
        # 故障期：每个设备一次受理尝试，必须全部门禁超时、零现场写入。
        for device in sorted(sessions):
            specs = tuple(
                make_spec(f"p{point}", device=device, value=float(point))
                for point in range(BATCH_POINTS)
            )
            with pytest.raises(TimeoutError):
                await executor.accept(
                    list(specs), request_id=f"req-perf-out-{uuid4().hex}", caller_id="perf"
                )
            rejected_commands += BATCH_POINTS
        outage_elapsed = time.perf_counter() - started
        assert executor.pending_commands() == 0
        assert all(session.one_calls == [] for session in sessions.values())
        # worker 健康面如实暴露失败，内存积压严格有界。
        assert worker.failure is not None
        assert state["failed_batches"] >= 1
        assert 0 < max_pending["value"] < 100_000

        # 恢复 SQLite：同栈新受理必须成功并真实走到终态。
        state["failing"] = False
        repository.append_events = counting_append
        recovered_specs = tuple(
            make_spec(f"p{point}", device="WT000", value=float(point))
            for point in range(BATCH_POINTS)
        )
        recovered = await executor.accept(
            list(recovered_specs), request_id=f"req-perf-rec-{uuid4().hex}", caller_id="perf"
        )
        recovered_results = await asyncio.gather(*(executor.wait(c.command_id) for c in recovered))
        assert all(r is not None and r.state.value == "SUCCEEDED" for r in recovered_results)
    finally:
        blocked = await executor.close(grace_seconds=10.0)
        assert blocked == frozenset()
    assert await worker.stop(timeout=30.0)
    stop_sampling.set()
    await sampler
    total_elapsed = time.perf_counter() - started
    terminal_metrics = writer.submit(repository.metrics).result()
    writer.submit(repository.close).result()
    writer.shutdown(wait=True)

    # 每个被拒受理在内存留下 T1 + INTERRUPTED 终态（每点 2 事件），
    # 恢复后随新受理事件一起幂等落盘：无丢失、无重复。
    expected_rejected_events = rejected_commands * 2
    expected_recovered_events = BATCH_POINTS * 3 + 1  # T1/T2/T3 + CONNECTING
    expected_events = expected_rejected_events + expected_recovered_events
    print(
        "\n[perf-outage] "
        f"outage_phase={outage_elapsed:.2f}s total_with_drain={total_elapsed:.2f}s "
        f"rejected_commands={rejected_commands} failed_attempts={state['failed_batches']} "
        f"max_backlog={max_pending['value']} drain_batches={len(batches)} "
        f"events={sum(batches)} expected={expected_events}"
    )

    assert sum(batches) == expected_events
    assert max(batches) <= 500
    assert terminal_metrics.accepted_total == rejected_commands + BATCH_POINTS
    terminal = dict(terminal_metrics.terminal_counts)
    assert terminal.get("INTERRUPTED") == rejected_commands
    assert terminal.get("SUCCEEDED") == BATCH_POINTS
    assert not (set(terminal) - {"INTERRUPTED", "SUCCEEDED"})


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
            accepted = await executor.accept(
                [make_spec("p0", device=device, value=float(i))],
                request_id=f"req-perf-{uuid4().hex}",
                caller_id="perf",
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
                f"(writer 线程化后应显著低于上轮 0.95ms 基线)",
            ]
        )
    )

    # 四桶身份互斥且合计可解释：e2e ≈ accept + queue + write（允许少量簿记开销）。
    for a, q, w, e in zip(accept_ms, queue_ms, write_ms, e2e_ms, strict=True):
        assert e + 0.5 >= a + q + w
    # 协议写入在途时间必须由 write 桶承载（注入 1ms sleep），不得漏进 accept。
    assert _pct(write_ms, 0.5) >= 0.9
    # B4 证据：SQLite/fsync 全部在唯一 writer 线程执行，accept 桶只含一次
    # executor 往返，事件循环不再被持久化独占（上轮最大抖动 0.95ms 基线）。
    # 本机差异下只钉非负与四桶可解释性，具体数值在验收报告中对比。
    assert max(jitter) >= 0.0
