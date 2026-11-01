"""write-behind 专项：内存状态原子性、后台落盘、故障注入、停机与恢复不变量。

所有用例使用真实 EventRepository（归属唯一单线程池）+ PersistenceWorker
+ 受控伪现场会话。核心不变量：

  T1/T2/T3 在事件循环线程的内存状态中同步生效；SQLite 只经后台 worker
  批量落盘。现场控制不被 SQLite 阻塞；已接收命令的终态事件容量在受理时
  预留；落盘失败保留快照、后台按序重试且幂等；异常崩溃允许丢失未落盘
  事件，但已落盘子集恢复时必须保守安全（UNKNOWN / NOT_SENT），绝不重发。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

from commander.application.control import CommandControl
from commander.application.journal_port import JournalPort
from commander.application.persistence import BufferedJournal, PersistenceWorker
from commander.application.queued_executor import QueuedExecutor
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
)
from commander.infrastructure.event_repository import EventRepository
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)

from ..conftest import flush_buffer
from .conftest import FakeSession, build_executor, make_spec, wait_for_result

pytestmark = pytest.mark.component

TERMINAL = {
    CommandState.SUCCEEDED,
    CommandState.FAILED,
    CommandState.UNKNOWN,
    CommandState.EXPIRED,
    CommandState.INTERRUPTED,
    CommandState.CANCELLED,
}


def repository_path(repo: EventRepository) -> Path:
    return repo._path


async def _durable_state(
    buffer: BufferedJournal,
    persistence: PersistenceWorker,
    repo: EventRepository,
    writer: ThreadPoolExecutor,
    command_id: str,
) -> CommandState:
    await flush_buffer(buffer, persistence)
    result = writer.submit(repo.get, command_id).result()
    assert result is not None
    return result.state


async def test_concurrent_accepts_across_devices_all_durable_and_settled(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """场景：多设备同时受理。内存无一丢失，worker 排空后全部落盘。"""
    devices = 32
    per_device = 5
    sessions = {f"D{i:02d}": FakeSession() for i in range(devices)}
    executor = build_executor(
        control,
        sessions,
        global_capacity=16,
        max_queued_batches_per_device=10,
        max_queued_batches_global=500,
    )

    async def run_device(device: str) -> list[AcceptedCommand]:
        accepted: list[AcceptedCommand] = []
        for _ in range(per_device):
            accepted.extend(await executor.accept([make_spec(device=device)], caller_id="stress"))
        return accepted

    try:
        accepted = await asyncio.gather(*(run_device(f"D{i:02d}") for i in range(devices)))
        flat = [command for group in accepted for command in group]
        assert len({c.command_id for c in flat}) == len(flat)
        await asyncio.gather(
            *(executor.wait(c.command_id) for c in flat),
            return_exceptions=True,
        )
    finally:
        assert await executor.close(grace_seconds=5.0) == frozenset()

    total = devices * per_device
    await flush_buffer(buffer, persistence)
    metrics = sqlite_writer.submit(repository.metrics).result()
    assert metrics.accepted_total == total
    assert dict(metrics.terminal_counts).get("SUCCEEDED") == total
    assert executor.pending_commands() == 0


def _fail_append_for(repo: EventRepository, *, fail_calls: int | None = None) -> dict[str, Any]:
    """故障闸：默认持续失败；fail_calls=N 时仅前 N 次 append_events 抛 sqlite3.Error。

    state["restore"]() 立即恢复。模拟 SQLite 暂时/长期不可用（事务级故障），
    worker 必须保留快照、重试、恢复后幂等落盘。
    """
    original = repo.append_events
    state: dict[str, Any] = {
        "calls": 0,
        "failed_batches": 0,
        "failing": True,
        "fail_calls": fail_calls,
    }

    def fault(events) -> None:  # 签名经 append_events 赋值点校验
        state["calls"] += 1
        if state["failing"]:
            if state["fail_calls"] is not None and state["calls"] > state["fail_calls"]:
                state["failing"] = False
            else:
                state["failed_batches"] += 1
                raise sqlite3.OperationalError("simulated disk I/O error")
        return original(events)

    def restore() -> None:
        state["failing"] = False

    state["restore"] = restore
    # 运行时替换实例方法（测试故障注入），绕过 mypy 对类方法赋值的静态限制。
    object.__setattr__(repo, "append_events", fault)
    return state


async def test_sqlite_outage_keeps_field_control_running_and_retries_in_order(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """SQLite 暂时不可用：已接收命令照常执行；快照保留、恢复后按序落盘不重复。

    取代旧契约"SQLite 故障即 fail-closed"——write-behind 下现场链路不得
    被持久化阻塞；worker 失败必须对外可见（failure/DEGRADED 由服务层观测）。
    """
    fault = _fail_append_for(repository, fail_calls=5)
    session = FakeSession()
    executor = build_executor(control, {"WT01": session})
    accepted = await executor.accept([make_spec()], caller_id="op")
    # 内存终态立即可查，现场写入真实发生，执行器不进入 fatal。
    result = await wait_for_result(executor, accepted[0])
    try:
        assert result.state is CommandState.SUCCEEDED
        assert session.one_calls == ["setpoint"]
        assert executor.operational is True
        # 故障期间 worker 如实暴露失败，事件滞留在内存。
        deadline = time.monotonic() + 2.0
        while persistence.failure is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert persistence.failure is not None
        # 新受理不受 SQLite 故障影响。
        accepted2 = await executor.accept([make_spec(point="p2")], caller_id="op")
        result2 = await wait_for_result(executor, accepted2[0])
        assert result2.state is CommandState.SUCCEEDED
    finally:
        await executor.close()

    # 恢复（第 6 次起成功）：全部事件按序落盘，无重复。
    await flush_buffer(buffer, persistence)
    assert fault["failing"] is False
    assert fault["failed_batches"] == 5
    metrics = sqlite_writer.submit(repository.metrics).result()
    assert metrics.accepted_total == 2
    assert dict(metrics.terminal_counts).get("SUCCEEDED") == 2
    assert persistence.failure is None
    rows = sqlite_writer.submit(
        lambda: repository._db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT event_id) " "FROM command_events"
        ).fetchone()
    ).result()
    # 4（首命令含 CONNECTING）+ 3（会话复用，无 CONNECTING），event_id 全唯一。
    assert rows == (7, 7)


def test_buffer_transition_batch_atomic_when_one_member_illegal(buffer) -> None:
    """内存 T2 批量必须原子：一个成员转移非法，整批不更新、不追加事件。"""
    accepted = buffer.accept([make_spec("p1"), make_spec("p2")], caller_id="op")
    pending_before = buffer.pending_count
    buffer.transition(accepted[0].command_id, CommandState.CONNECTING)
    with pytest.raises(Exception):  # noqa: B017, BLE001
        buffer.transition_many(
            (
                (accepted[0].command_id, CommandState.EXECUTING, None, None),
                # p2 仍为 ACCEPTED：直接 SUCCEEDED 非法。
                (
                    accepted[1].command_id,
                    CommandState.SUCCEEDED,
                    CommandOutcome.CONFIRMED_SUCCESS,
                    None,
                ),
            )
        )
    # 整批回滚：p1 停在 CONNECTING（无 T2），p2 停在 ACCEPTED；无事件追加。
    assert buffer.get_cached(accepted[0].command_id).state is CommandState.CONNECTING
    assert buffer.get_cached(accepted[1].command_id).state is CommandState.ACCEPTED
    assert buffer.pending_count == pending_before + 1


def test_repository_transition_many_atomic_rollback_when_one_member_illegal(
    repository, sqlite_writer
) -> None:
    """仓储级直接批量事务：一个成员非法，整批 ROLLBACK，无部分落盘。"""

    def seed() -> tuple[str, str]:
        accepted = repository.accept([make_spec("p1"), make_spec("p2")], caller_id="op")
        repository.transition(accepted[0].command_id, CommandState.CONNECTING)
        return accepted[0].command_id, accepted[1].command_id

    c1, c2 = sqlite_writer.submit(seed).result()
    with pytest.raises(Exception):  # noqa: B017, BLE001
        sqlite_writer.submit(
            repository.transition_many,
            (
                (c1, CommandState.EXECUTING, None, None),
                (c2, CommandState.SUCCEEDED, CommandOutcome.CONFIRMED_SUCCESS, None),
            ),
        ).result()
    result = sqlite_writer.submit(repository.get, c1).result()
    assert result is not None and result.state is CommandState.CONNECTING
    result = sqlite_writer.submit(repository.get, c2).result()
    assert result is not None and result.state is CommandState.ACCEPTED


class _ObservingManySession(FakeSession):
    """进入原生多写时逐成员核对内存 T2 已生效（内存 T2 先于现场写）。"""

    def __init__(self, lookup: dict[str, str], buffer: BufferedJournal) -> None:
        super().__init__(
            capabilities=frozenset(
                {ProtocolCapability.READ, ProtocolCapability.WRITE, ProtocolCapability.WRITE_MANY}
            )
        )
        self._lookup = lookup
        self._buffer = buffer
        self.memory_at_write: dict[str, CommandState] = {}

    async def write_many(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
        for write in writes:
            result = self._buffer.get_cached(self._lookup[write.point])
            assert result is not None
            self.memory_at_write[write.point] = result.state
        return await super().write_many(writes)


async def test_memory_t2_is_recorded_before_any_native_write(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """旧契约"SQLite COMMIT 前不得 PLC"的迁移：内存 T2 记录前不得现场写。

    SQLite 落盘与现场写解耦；判定点改为内存状态——write_many 入口处
    每个成员必须已是 EXECUTING，且此刻持久层可能尚无 T2（落盘异步）。
    """
    accepted_ids: dict[str, str] = {}
    holder: dict[str, _ObservingManySession] = {}

    def factory(device_id: str) -> _ObservingManySession:
        session = _ObservingManySession(accepted_ids, buffer)
        holder[device_id] = session
        return session

    executor = QueuedExecutor(
        control,
        factory,
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        max_batch_points=10,
    )
    accepted = await executor.accept(
        [make_spec("p1"), make_spec("p2"), make_spec("p3")], caller_id="op"
    )
    accepted_ids.update({c.spec.point_id: c.command_id for c in accepted})
    try:
        results = await asyncio.gather(*(executor.wait(c.command_id) for c in accepted))
    finally:
        await executor.close()
    assert results
    for result in results:
        assert result is not None
        assert result.state is CommandState.SUCCEEDED
    # 进入现场多写的瞬间，三个成员在内存中均已记录 T2。
    assert holder["WT01"].many_calls == [("p1", "p2", "p3")]
    assert holder["WT01"].memory_at_write == {
        "p1": CommandState.EXECUTING,
        "p2": CommandState.EXECUTING,
        "p3": CommandState.EXECUTING,
    }


async def test_terminal_decisions_settle_in_memory_under_sqlite_outage(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """旧契约"T3 事务故障 fail-closed"的迁移：现场已写则内存如实终态。

    落盘长期失败期间，调用方必须立即拿到正确终态、现场只写一次；
    SQLite 恢复后落盘终态与内存一致。
    """
    fault = _fail_append_for(repository)  # 默认持续失败，显式恢复前一直不可用
    session = FakeSession(
        capabilities=frozenset(
            {ProtocolCapability.READ, ProtocolCapability.WRITE, ProtocolCapability.WRITE_MANY}
        )
    )
    executor = build_executor(control, {"WT01": session}, max_batch_points=10)
    accepted = await executor.accept([make_spec("p1"), make_spec("p2")], caller_id="op")
    results = await asyncio.gather(*(executor.wait(c.command_id) for c in accepted))
    assert results
    settled = [r for r in results if r is not None]
    assert len(settled) == len(results)
    try:
        assert {r.state for r in settled} == {CommandState.SUCCEEDED}
        assert session.many_calls == [("p1", "p2")]
        assert executor.operational is True
        # 事件全部滞留内存：终态可查、缓存未被提前回收。
        memory = {c.command_id: buffer.get_cached(c.command_id).state for c in accepted}
        assert set(memory.values()) == {CommandState.SUCCEEDED}
        # 恢复 SQLite：worker 下一轮重试成功，停机排空。
        fault["restore"]()
    finally:
        await executor.close()
    await flush_buffer(buffer, persistence)
    metrics = sqlite_writer.submit(repository.metrics).result()
    assert dict(metrics.terminal_counts).get("SUCCEEDED") == 2


async def test_queue_timer_racing_t2_reaches_terminal_then_durable(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """排队计时器与 T2 竞争（多轮扰动）：内存终态与排空后落盘逐命令一致。"""
    for round_no in range(12):
        sessions = {"A": FakeSession(write_delay=0.01 + 0.003 * round_no), "B": FakeSession()}
        executor = build_executor(
            control,
            sessions,
            global_capacity=1,
            queue_timeout=0.03 + 0.002 * round_no,
            connect_timeout=1.0,
            write_timeout=1.0,
        )
        accepted_a = await executor.accept([make_spec(device="A")], caller_id="op")
        accepted_bs: list[AcceptedCommand] = []
        for _ in range(2):
            accepted_bs.extend(await executor.accept([make_spec(device="B")], caller_id="op"))
        await asyncio.sleep(0.004 * round_no)
        all_accepted = accepted_a + tuple(accepted_bs)
        results = await asyncio.gather(
            *(executor.wait(c.command_id) for c in all_accepted),
            return_exceptions=True,
        )
        blocked = await executor.close(grace_seconds=3.0)
        assert blocked == frozenset()
        succeeded = 0
        for command, result in zip(all_accepted, results, strict=True):
            assert not isinstance(result, BaseException)
            assert result is not None and result.state in TERMINAL
            durable = await _durable_state(
                buffer, persistence, repository, sqlite_writer, command.command_id
            )
            assert durable in TERMINAL
            assert durable is result.state
            if durable is CommandState.SUCCEEDED:
                succeeded += 1
        assert len(sessions["A"].one_calls) + len(sessions["B"].one_calls) == succeeded


async def test_close_settles_memory_terminals_then_drain_makes_them_durable(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """停机：在途命令先在内存终结；受控停机排空后持久层无 ACCEPTED/EXECUTING 残留。"""
    sessions = {f"D{i}": FakeSession(connect_delay=0.02, write_delay=0.03) for i in range(8)}
    executor = build_executor(
        control, sessions, global_capacity=8, write_timeout=2.0, connect_timeout=2.0
    )
    accepted: list[AcceptedCommand] = []
    for i in range(8):
        accepted.extend(await executor.accept([make_spec(device=f"D{i}")], caller_id="op"))
    await asyncio.sleep(0.01)
    blocked = await executor.close(grace_seconds=5.0)
    assert blocked == frozenset()
    for command in accepted:
        memory = buffer.get_cached(command.command_id)
        assert memory is not None and memory.state in TERMINAL
    await flush_buffer(buffer, persistence)
    metrics = sqlite_writer.submit(repository.metrics).result()
    assert metrics.accepted_total == 8
    assert sum(dict(metrics.terminal_counts).values()) == 8


async def test_repeated_accepts_under_worker_flush_preserve_fifo(
    control, buffer, persistence
) -> None:
    """FIFO/全局额度在后台批量落盘压力下不被破坏。"""
    max_concurrency = 2
    import threading

    seen = {"active": 0, "peak": 0}
    lock = threading.Lock()

    class GateSession(FakeSession):
        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            with lock:
                seen["active"] += 1
                seen["peak"] = max(seen["peak"], seen["active"])
            try:
                await asyncio.sleep(0.02)
                return await super().write_one(write)
            finally:
                with lock:
                    seen["active"] -= 1

    sessions = {"slow": GateSession(), "fast": GateSession()}
    executor = build_executor(control, sessions, global_capacity=max_concurrency)
    slow = [
        (await executor.accept([make_spec(f"s{i}", device="slow")], caller_id="op"))[0]
        for i in range(4)
    ]
    fast = (await executor.accept([make_spec("f0", device="fast")], caller_id="op"))[0]
    try:
        results = await asyncio.gather(*(executor.wait(c.command_id) for c in [*slow, fast]))
    finally:
        assert await executor.close(grace_seconds=5.0) == frozenset()
    succeeded = [r for r in results if isinstance(r, CommandResult)]
    assert len(succeeded) == len(results)
    assert all(r.state is CommandState.SUCCEEDED for r in succeeded)
    assert sessions["slow"].one_calls == ["s0", "s1", "s2", "s3"]
    assert seen["peak"] <= max_concurrency


async def test_terminal_outcome_mapping_consistent_for_all_settlement_paths(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """EXPIRED/FAILED 终态均带 COMPLETED 事件投影与一致 outcome，落盘后同样一致。"""
    gate = asyncio.Event()

    class Hang(FakeSession):
        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            await gate.wait()
            return await super().write_one(write)

    sessions = {"d1": Hang(), "d2": FakeSession(connect_delay=0.0)}
    executor = build_executor(
        control,
        sessions,
        global_capacity=1,
        queue_timeout=0.05,
        connect_timeout=1.0,
        write_timeout=5.0,
    )
    first = await executor.accept([make_spec(device="d1")], caller_id="op")
    await asyncio.sleep(0.02)
    second = await executor.accept([make_spec(device="d2")], caller_id="op")
    expired = await wait_for_result(executor, second[0], timeout=2.0)
    assert expired.state is CommandState.EXPIRED
    assert expired.outcome is CommandOutcome.NOT_SENT
    gate.set()
    await executor.wait(first[0].command_id)
    blocked = await executor.close(grace_seconds=3.0)
    assert blocked == frozenset()
    await flush_buffer(buffer, persistence)
    counts = dict(sqlite_writer.submit(repository.metrics).result().terminal_counts)
    assert counts.get("EXPIRED") == 1
    assert counts.get("SUCCEEDED") == 1


class _ScriptedPointSession(FakeSession):
    """无 WRITE_MANY 的 Modbus 型会话：逐点按脚本返回成功/拒绝/异常/挂起。"""

    def __init__(self, script: dict[str, Any]) -> None:
        super().__init__()  # 仅 READ/WRITE，无 WRITE_MANY
        self._script = script

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        action = self._script[write.point]
        self.one_calls.append(write.point)
        if isinstance(action, Exception):
            raise action
        return ProtocolWriteResult(write.point, action)


async def test_serial_batch_mixed_confirmed_outcomes_recorded_per_point(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """无 WRITE_MANY 共享批次：各点确认结果不同（成功/明确拒绝）必须逐点裁决。"""
    session = _ScriptedPointSession(
        {
            "p0": WriteOutcome.CONFIRMED_SUCCESS,
            "p1": WriteOutcome.CONFIRMED_FAILURE,
            "p2": WriteOutcome.CONFIRMED_SUCCESS,
        }
    )
    executor = build_executor(control, {"WT01": session}, max_batch_points=10)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")], caller_id="op"
    )
    raw_results = await asyncio.gather(*(executor.wait(c.command_id) for c in accepted))
    results: list[CommandResult] = []
    for result in raw_results:
        assert result is not None
        results.append(result)
    try:
        states = {
            command.spec.point_id: result.state
            for command, result in zip(accepted, results, strict=True)
        }
        outcomes = {
            command.spec.point_id: result.outcome
            for command, result in zip(accepted, results, strict=True)
        }
        assert states == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.FAILED,
            "p2": CommandState.SUCCEEDED,
        }
        assert outcomes["p1"] is CommandOutcome.CONFIRMED_FAILURE
        assert session.one_calls == ["p0", "p1", "p2"]
        assert session.many_calls == []
    finally:
        await executor.close()
    await flush_buffer(buffer, persistence)
    durable = {
        c.spec.point_id: sqlite_writer.submit(repository.get, c.command_id).result()
        for c in accepted
    }
    assert {r.state for r in durable.values()} == {
        CommandState.SUCCEEDED,
        CommandState.FAILED,
    }


async def test_serial_batch_midpoint_exception_keeps_earlier_confirmed_evidence(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """逐点写第 3 点抛异常：前两点协议确认保留，中断点 UNKNOWN、后缀 NOT_SENT。"""
    session = _ScriptedPointSession(
        {
            "p0": WriteOutcome.CONFIRMED_SUCCESS,
            "p1": WriteOutcome.CONFIRMED_FAILURE,
            "p2": OSError("link reset mid-batch"),
            "p3": WriteOutcome.CONFIRMED_SUCCESS,
        }
    )
    executor = build_executor(control, {"WT01": session}, max_batch_points=10)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2"), make_spec("p3")],
        caller_id="op",
    )
    await asyncio.gather(
        *(executor.wait(c.command_id) for c in accepted),
        return_exceptions=True,
    )
    try:
        states = {c.spec.point_id: buffer.get_cached(c.command_id).state for c in accepted}
        assert states["p0"] is CommandState.SUCCEEDED
        assert states["p1"] is CommandState.FAILED
        assert states["p2"] is CommandState.UNKNOWN
        assert states["p3"] is CommandState.INTERRUPTED
        assert session.one_calls == ["p0", "p1", "p2"]
        assert session.many_calls == []
    finally:
        await executor.close()
    await flush_buffer(buffer, persistence)
    durable = {
        c.spec.point_id: sqlite_writer.submit(repository.get, c.command_id).result()
        for c in accepted
    }
    assert durable["p0"].state is CommandState.SUCCEEDED
    assert durable["p1"].state is CommandState.FAILED
    assert durable["p1"].outcome is CommandOutcome.CONFIRMED_FAILURE
    assert durable["p2"].state is CommandState.UNKNOWN
    assert durable["p3"].state is CommandState.INTERRUPTED
    assert durable["p3"].outcome is CommandOutcome.NOT_SENT


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (
            {
                "p0": WriteOutcome.CONFIRMED_SUCCESS,
                "p1": ConnectionError("link lost"),
                "p2": WriteOutcome.CONFIRMED_SUCCESS,
            },
            (
                CommandState.SUCCEEDED,
                CommandState.UNKNOWN,
                CommandState.INTERRUPTED,
            ),
        ),
        (
            {
                "p0": WriteOutcome.CONFIRMED_FAILURE,
                "p1": OSError("reset"),
                "p2": WriteOutcome.CONFIRMED_SUCCESS,
            },
            (CommandState.FAILED, CommandState.UNKNOWN, CommandState.INTERRUPTED),
        ),
        (
            {
                "p0": ConnectionError("link lost"),
                "p1": WriteOutcome.CONFIRMED_SUCCESS,
                "p2": WriteOutcome.CONFIRMED_SUCCESS,
            },
            (
                CommandState.UNKNOWN,
                CommandState.INTERRUPTED,
                CommandState.INTERRUPTED,
            ),
        ),
    ],
)
async def test_serial_batch_failure_matrix_per_point_adjudication(
    control, repository, sqlite_writer, buffer, persistence, script, expected
) -> None:
    """N1 故障矩阵：异常位置不同，逐点终态严格匹配现场证据，落盘后一致。"""
    session = _ScriptedPointSession(script)
    executor = build_executor(control, {"WT01": session}, max_batch_points=10, write_timeout=2.0)
    points = ["p0", "p1", "p2"]
    accepted = await executor.accept([make_spec(point) for point in points], caller_id="op")
    await asyncio.gather(
        *(executor.wait(c.command_id) for c in accepted),
        return_exceptions=True,
    )
    try:
        memory = tuple(buffer.get_cached(c.command_id).state for c in accepted)
        assert memory == expected
        failure_index = next(
            i for i, point in enumerate(points) if isinstance(script[point], Exception)
        )
        assert session.one_calls == points[: failure_index + 1]
    finally:
        await executor.close()
    actual = await asyncio.gather(
        *(
            _durable_state(buffer, persistence, repository, sqlite_writer, c.command_id)
            for c in accepted
        )
    )
    assert tuple(actual) == expected


@pytest.mark.parametrize(
    ("script", "expected", "attempted"),
    [
        (
            {
                "p0": WriteOutcome.CONFIRMED_SUCCESS,
                "p1": WriteOutcome.UNKNOWN,
                "p2": WriteOutcome.CONFIRMED_SUCCESS,
            },
            (
                CommandState.SUCCEEDED,
                CommandState.UNKNOWN,
                CommandState.INTERRUPTED,
            ),
            ["p0", "p1"],
        ),
        (
            {
                "p0": WriteOutcome.UNKNOWN,
                "p1": WriteOutcome.CONFIRMED_SUCCESS,
                "p2": WriteOutcome.CONFIRMED_SUCCESS,
            },
            (
                CommandState.UNKNOWN,
                CommandState.INTERRUPTED,
                CommandState.INTERRUPTED,
            ),
            ["p0"],
        ),
    ],
)
async def test_serial_batch_returned_unknown_matrix(
    control, repository, sqlite_writer, buffer, persistence, script, expected, attempted
) -> None:
    """矩阵：write_one 返回 UNKNOWN 时后续点按 NOT_SENT 裁决，落盘投影一致。"""
    session = _ScriptedPointSession(script)
    executor = build_executor(control, {"WT01": session}, max_batch_points=10)
    points = ["p0", "p1", "p2"]
    accepted = await executor.accept([make_spec(point) for point in points], caller_id="op")
    await asyncio.gather(
        *(executor.wait(c.command_id) for c in accepted),
        return_exceptions=True,
    )
    try:
        assert tuple(buffer.get_cached(c.command_id).state for c in accepted) == expected
        assert session.one_calls == attempted
    finally:
        await executor.close()
    await flush_buffer(buffer, persistence)
    outcomes = {
        c.spec.point_id: sqlite_writer.submit(repository.get, c.command_id).result().outcome
        for c in accepted
    }
    for point, state in zip(points, expected, strict=True):
        if state is CommandState.INTERRUPTED:
            assert outcomes[point] is CommandOutcome.NOT_SENT


class _HangThenResumeSession(FakeSession):
    """p1 的 write_one 挂起直到外部放行：模拟写超时但底层 I/O 仍存活。"""

    def __init__(self) -> None:
        super().__init__()
        self.resume = asyncio.Event()

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        self.one_calls.append(write.point)
        if write.point == "p1":
            await self.resume.wait()
        return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)


async def test_serial_batch_write_timeout_marks_unsettled_suffix_unknown_without_overwrite(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """超时后 shield 底层任务继续：未确认点 UNKNOWN；物理成功不得覆盖终态。"""
    session = _HangThenResumeSession()
    executor = build_executor(control, {"WT01": session}, max_batch_points=10, write_timeout=0.3)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")], caller_id="op"
    )
    try:
        await asyncio.gather(
            *(executor.wait(c.command_id) for c in accepted),
            return_exceptions=True,
        )
        states_at_timeout = {
            c.spec.point_id: buffer.get_cached(c.command_id).state for c in accepted
        }
        assert states_at_timeout == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.UNKNOWN,
            "p2": CommandState.UNKNOWN,
        }
        assert session.one_calls == ["p0", "p1"]
        session.resume.set()
        deadline = time.monotonic() + 3.0
        while "p2" not in session.one_calls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert session.one_calls == ["p0", "p1", "p2"]
        states_after_settle = {
            c.spec.point_id: buffer.get_cached(c.command_id).state for c in accepted
        }
        assert states_after_settle == states_at_timeout
    finally:
        await executor.close(grace_seconds=10.0)
    await flush_buffer(buffer, persistence)
    durable = {
        c.spec.point_id: sqlite_writer.submit(repository.get, c.command_id).result()
        for c in accepted
    }
    assert {r.state for r in durable.values()} == {
        CommandState.SUCCEEDED,
        CommandState.UNKNOWN,
    }


async def test_serial_batch_returned_unknown_outcome_suffix_is_not_sent(
    control, buffer, persistence
) -> None:
    """中间点返回 UNKNOWN：当前点 UNKNOWN，其后确定未调用点 INTERRUPTED/NOT_SENT。"""
    session = _ScriptedPointSession(
        {
            "p0": WriteOutcome.CONFIRMED_SUCCESS,
            "p1": WriteOutcome.UNKNOWN,
            "p2": WriteOutcome.CONFIRMED_SUCCESS,
        }
    )
    executor = build_executor(control, {"WT01": session}, max_batch_points=10)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")], caller_id="op"
    )
    await asyncio.gather(
        *(executor.wait(c.command_id) for c in accepted),
        return_exceptions=True,
    )
    try:
        states = {c.spec.point_id: buffer.get_cached(c.command_id).state for c in accepted}
        assert states == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.UNKNOWN,
            "p2": CommandState.INTERRUPTED,
        }
        assert session.one_calls == ["p0", "p1"]
    finally:
        await executor.close()


def _seed_durable_chain(repo: EventRepository, *, executing: bool) -> str:
    accepted = repo.accept([make_spec("p1")], caller_id="op")
    command_id = accepted[0].command_id
    repo.transition(command_id, CommandState.CONNECTING)
    if executing:
        repo.transition(command_id, CommandState.EXECUTING)
    return command_id


def _seed_accepted_only(repo: EventRepository) -> str:
    return repo.accept([make_spec("p2")], caller_id="op")[0].command_id


async def test_recover_executing_is_unknown_and_accepted_only_is_interrupted(
    tmp_path, sqlite_writer
) -> None:
    """崩溃恢复两分支：durable EXECUTING→UNKNOWN；仅 durable ACCEPTED→INTERRUPTED。

    两种情况下都绝不重发现场命令（recover 只写终态事件）。
    """
    db_path = tmp_path / "recover.db"
    repo = sqlite_writer.submit(lambda: EventRepository(db_path)).result()
    executing_id = sqlite_writer.submit(lambda: _seed_durable_chain(repo, executing=True)).result()
    accepted_id = sqlite_writer.submit(lambda: _seed_accepted_only(repo)).result()
    try:
        rows = sqlite_writer.submit(
            lambda: repo._db.execute("SELECT COUNT(*) FROM command_events").fetchone()
        ).result()
        assert rows[0] == 4  # ACCEPTED+CONNECTING+EXECUTING 与单 ACCEPTED
    finally:
        sqlite_writer.submit(repo.close).result()

    reopen = sqlite_writer.submit(lambda: EventRepository(db_path)).result()
    try:
        recovered = set(sqlite_writer.submit(reopen.recover).result())
        assert recovered == {executing_id, accepted_id}
        executing_result = sqlite_writer.submit(reopen.get, executing_id).result()
        accepted_result = sqlite_writer.submit(reopen.get, accepted_id).result()
        assert executing_result.state is CommandState.UNKNOWN
        assert executing_result.outcome is CommandOutcome.UNKNOWN
        assert accepted_result.state is CommandState.INTERRUPTED
        assert accepted_result.outcome is CommandOutcome.NOT_SENT
    finally:
        sqlite_writer.submit(reopen.close).result()


async def test_crash_before_flush_loses_volatile_events_without_replay(
    tmp_path, sqlite_writer
) -> None:
    """异常崩溃允许丢失未落盘事件：重开后库里没有该命令，也绝不重发。"""
    db_path = tmp_path / "crash.db"
    repo = sqlite_writer.submit(lambda: EventRepository(db_path)).result()
    buffer = BufferedJournal()
    # 超长周期：受理后 worker 来不及任何落盘即"进程崩溃"。
    worker = PersistenceWorker(buffer, repo, sqlite_writer, interval=60.0, batch_size=500)
    worker.start()
    physical: list[str] = []

    class CrashSession(FakeSession):
        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            physical.append(write.point)
            return await super().write_one(write)

    control = CommandControl(repo, sqlite_writer, buffer)
    executor = build_executor(control, {"WT01": CrashSession()})
    accepted = await executor.accept([make_spec()], caller_id="op")
    await executor.wait(accepted[0].command_id)
    # 模拟进程被 SIGKILL：不排空、直接取消 worker，关闭仓储。
    assert worker._task is not None
    worker._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker._task
    assert buffer.pending_count == 4  # 4 个事件全部滞留内存
    await executor.close()
    sqlite_writer.submit(repo.close).result()

    # 重启：库里无该命令；recover 返回空；现场未被任何人重发。
    reopen = sqlite_writer.submit(lambda: EventRepository(db_path)).result()
    try:
        assert sqlite_writer.submit(reopen.recover).result() == ()
        assert sqlite_writer.submit(reopen.metrics).result().accepted_total == 0
    finally:
        sqlite_writer.submit(reopen.close).result()
    assert physical == ["setpoint"]


def test_invalid_buffer_and_worker_configuration_rejected() -> None:
    """边界/非法配置：容量与批参数必须 fail-fast。"""
    with pytest.raises(ValueError):
        BufferedJournal(maximum_events=0)
    with pytest.raises(ValueError):
        BufferedJournal(initial_sequence=-1)
    # 校验在使用 repository/writer 之前 fail-fast；cast 仅满足构造签名。
    dummy_journal = cast(JournalPort, None)
    dummy_executor = cast(Executor, None)
    with pytest.raises(ValueError):
        PersistenceWorker(BufferedJournal(), dummy_journal, dummy_executor, interval=0)
    with pytest.raises(ValueError):
        PersistenceWorker(BufferedJournal(), dummy_journal, dummy_executor, batch_size=0)


async def test_queue_full_rejects_new_commands_but_accepted_ones_terminate(
    repository, sqlite_writer
) -> None:
    """容量预留 + SQLite 长期不可用：队列满拒新、已受理命令终结不被阻塞、内存有界。

    用极小容量（每命令预留 4 事件）直接构造 buffer/control/executor；
    append_events 永久失败模拟长期故障，排除后台排空造成的竞态。
    """
    fault = _fail_append_for(repository)  # 不恢复：长期不可用
    tiny = BufferedJournal(maximum_events=8)  # 仅够 2 个命令的全生命周期
    worker = PersistenceWorker(tiny, repository, sqlite_writer, interval=0.02, batch_size=500)
    worker.start()
    tiny_control = CommandControl(repository, sqlite_writer, tiny)
    sessions = {"WT01": FakeSession(), "WT02": FakeSession()}
    executor = build_executor(tiny_control, sessions, max_batch_points=10)
    try:
        first = await executor.accept(
            [make_spec("p0", device="WT01"), make_spec("p1", device="WT02")],
            caller_id="op",
        )
        # 容量耗尽（8/8：T1 已入 pending，每命令余 3 预留）：新命令必须被拒。
        with pytest.raises(OverflowError):
            await executor.accept([make_spec("p2", device="WT01")], caller_id="op")
        # 已受理两命令正常走到终态（预留容量保证 T2/T3 入队），现场不被 SQLite 阻塞。
        results = await asyncio.gather(*(executor.wait(c.command_id) for c in first))
        assert results and all(r is not None for r in results)
        assert {r.state for r in results if r is not None} == {CommandState.SUCCEEDED}
        assert tiny.pending_count == 8
        # 等过 worker 周期：失败对健康可见、事件原样保留、无丢失无无限增长。
        deadline = time.monotonic() + 1.0
        while worker.failure is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker.failure is not None
        assert fault["failed_batches"] >= 1
        assert tiny.pending_count == 8
        # 长期故障下内存严格有界：再来的受理一律拒绝，不存在无界积压。
        with pytest.raises(OverflowError):
            await executor.accept([make_spec("p3", device="WT01")], caller_id="op")
    finally:
        fault["restore"]()
        await executor.close()
        assert await worker.stop(timeout=5.0)


async def test_batch_threshold_flushes_back_to_back_without_interval_wait(
    repository, sqlite_writer
) -> None:
    """达到批量阈值后连续排空：首批之后不得再等一个 interval 才发第二批。"""
    flushed: list[int] = []
    original = repository.append_events

    def counting(events) -> None:
        flushed.append(len(events))
        original(events)

    repository.append_events = counting
    buffer = BufferedJournal()
    worker = PersistenceWorker(buffer, repository, sqlite_writer, interval=0.2, batch_size=4)
    worker.start()
    try:
        # 同步灌入 8 个 T1 事件；worker 协程在这些同步调用之间无法插入。
        for index in range(8):
            buffer.accept([CommandSpec("WT01", f"p{index}", 1.0, "ref")], caller_id="op")
        started = time.monotonic()
        while buffer.pending_count and time.monotonic() - started < 2.0:
            await asyncio.sleep(0.005)
        assert buffer.pending_count == 0
        # 阈值分支跳过 interval 睡眠：两批背靠背，总耗时远小于一个 interval。
        assert time.monotonic() - started < 0.15
        assert flushed == [4, 4]
    finally:
        repository.append_events = original
        assert await worker.stop(timeout=5.0)


async def test_worker_unexpected_exit_is_visible_and_stop_reports_false(
    buffer, repository, sqlite_writer
) -> None:
    """worker 任务异常退出：operational 立刻不可用，stop 如实返回 False。"""
    worker = PersistenceWorker(buffer, repository, sqlite_writer, interval=0.05)
    worker.start()
    await asyncio.sleep(0.02)
    assert worker.operational
    assert worker._task is not None
    worker._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker._task
    assert worker.operational is False
    assert await worker.stop(timeout=1.0) is False


async def test_stop_interrupts_coalescing_window_without_waiting_interval(
    buffer, repository, sqlite_writer
) -> None:
    """停机必须立即中断合并窗口：排空耗时不得绑定 interval 本身。"""
    worker = PersistenceWorker(buffer, repository, sqlite_writer, interval=5.0, batch_size=500)
    worker.start()
    buffer.accept([CommandSpec("WT01", "p1", 1.0, "ref")], caller_id="op")
    await asyncio.sleep(0.05)  # 确保 worker 已进入 5s 合并等待。
    started = time.monotonic()
    assert await worker.stop(timeout=5.0)
    assert time.monotonic() - started < 1.0
    assert buffer.pending_count == 0
