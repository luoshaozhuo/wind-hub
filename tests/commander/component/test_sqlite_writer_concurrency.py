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
from uuid import uuid4

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
from commander.infrastructure.event_repository import EventRepository, RepositoryError
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)

from ..conftest import flush_buffer
from ..seed_events import seed_accept, seed_transition
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
            accepted.extend(
                await executor.accept(
                    [make_spec(device=device)],
                    request_id=f"req-swc-{uuid4().hex}",
                    caller_id="stress",
                )
            )
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


async def test_transient_sqlite_outage_blocks_admission_until_commit_then_settles_in_order(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """SQLite 暂时不可用（v1_22 RR-01）：受理阻塞到 T1 COMMIT，恢复后按序落盘不重复。

    accept 在 wait_persisted 处等待后台 worker 重试成功；返回保证 T1 已 durable，
    之后现场链路才允许写入。worker 失败对外可见（failure/DEGRADED 由服务层观测），
    快照保留、重试幂等；故障恢复后全部事件按序落盘、event_id 无重复。
    """
    fault = _fail_append_for(repository, fail_calls=5)
    session = FakeSession()
    executor = build_executor(control, {"WT01": session})
    started = time.monotonic()
    accepted = await executor.accept(
        [make_spec()], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
    # RR-01：受理返回经历了 worker 失败重试（5 个失败批次 × interval），
    # 且返回时刻 T1 必须已经 durable——现场 I/O 不可能先于 COMMIT 发生。
    assert time.monotonic() - started >= 0.05
    durable = sqlite_writer.submit(repository.get, accepted[0].command_id).result()
    assert durable is not None and durable.state is CommandState.ACCEPTED
    # 内存终态立即可查，现场写入真实发生，执行器不进入 fatal。
    result = await wait_for_result(executor, accepted[0])
    try:
        assert result.state is CommandState.SUCCEEDED
        assert session.one_calls == ["setpoint"]
        assert executor.operational is True
        # 受理能返回本身说明 worker 经历了完整失败-重试序列：故障闸计数为证，
        # 恢复后 worker.failure 已清除（瞬时故障的终态）。
        assert fault["failed_batches"] == 5
        assert persistence.failure is None
        # 故障恢复后的新受理：同样先 durable 再返回，正常收敛。
        accepted2 = await executor.accept(
            [make_spec(point="p2")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
        )
        durable2 = sqlite_writer.submit(repository.get, accepted2[0].command_id).result()
        assert durable2 is not None and durable2.state is CommandState.ACCEPTED
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
    accepted = buffer.accept(
        [make_spec("p1"), make_spec("p2")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
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


def test_repository_append_events_atomic_rollback_on_invalid_event(
    repository, sqlite_writer, buffer
) -> None:
    """Write-behind batch rolls back completely if a later event violates SQLite schema."""
    accepted = buffer.accept(
        [make_spec("p1"), make_spec("p2")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
    sqlite_writer.submit(repository.append_events, buffer.snapshot(100)).result()
    valid = {
        "event_id": "valid-connecting-event",
        "command_id": accepted[0].command_id,
        "event_type": "CONNECTING",
        "occurred_at": accepted[0].received_at.isoformat(),
        "state": CommandState.CONNECTING.value,
    }
    invalid = {
        **valid,
        "event_id": "invalid-event-type",
        "event_type": "NOT_AN_EVENT",
        "command_id": accepted[1].command_id,
    }
    with pytest.raises(RepositoryError):
        sqlite_writer.submit(repository.append_events, (valid, invalid)).result()
    first = sqlite_writer.submit(repository.get, accepted[0].command_id).result()
    second = sqlite_writer.submit(repository.get, accepted[1].command_id).result()
    assert first is not None and first.state is CommandState.ACCEPTED
    assert second is not None and second.state is CommandState.ACCEPTED


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
        [make_spec("p1"), make_spec("p2"), make_spec("p3")],
        request_id=f"req-swc-{uuid4().hex}",
        caller_id="op",
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


async def test_persistent_sqlite_outage_rejects_admission_without_field_io(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """SQLite 长期不可用（v1_22 §9.5.1）：持久化门禁超时 fail-closed，零现场写入。

    accept 在 accept_timeout 内等不到 T1 COMMIT 即抛 TimeoutError，不注册设备
    队列、不产生任何现场 I/O；保守 INTERRUPTED/NOT_SENT（PERSISTENCE_TIMEOUT）
    终态事件滞留内存。worker 失败对外可见；SQLite 恢复后滞留事件幂等落盘，
    持久层终态与内存裁决一致。
    """
    fault = _fail_append_for(repository)  # 默认持续失败，显式恢复前一直不可用
    gated = CommandControl(repository, sqlite_writer, buffer, accept_timeout=0.3)
    session = FakeSession(
        capabilities=frozenset(
            {ProtocolCapability.READ, ProtocolCapability.WRITE, ProtocolCapability.WRITE_MANY}
        )
    )
    executor = build_executor(gated, {"WT01": session}, max_batch_points=10)
    try:
        with pytest.raises(TimeoutError):
            await executor.accept(
                [make_spec("p1"), make_spec("p2")],
                request_id=f"req-swc-{uuid4().hex}",
                caller_id="op",
            )
        # 门禁拒绝：无队列条目、无任何现场会话写入，执行器仍可观测。
        assert executor.pending_commands() == 0
        assert session.many_calls == []
        assert session.one_calls == []
        assert executor.operational is True
        # 故障期间 worker 如实暴露失败。
        deadline = time.monotonic() + 2.0
        while persistence.failure is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert persistence.failure is not None
    finally:
        fault["restore"]()
        await executor.close()
    # 恢复 SQLite：滞留的 T1 + 保守终态事件幂等落盘。
    await flush_buffer(buffer, persistence)
    metrics = sqlite_writer.submit(repository.metrics).result()
    assert metrics.accepted_total == 2
    terminal = dict(metrics.terminal_counts)
    assert terminal.get("INTERRUPTED") == 2
    rows = sqlite_writer.submit(
        lambda: repository._db.execute(
            "SELECT DISTINCT outcome, error_code FROM command_events " "WHERE state='INTERRUPTED'"
        ).fetchall()
    ).result()
    assert rows == [("NOT_SENT", "PERSISTENCE_TIMEOUT")]


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
        accepted_a = await executor.accept(
            [make_spec(device="A")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
        )
        accepted_bs: list[AcceptedCommand] = []
        for _ in range(2):
            accepted_bs.extend(
                await executor.accept(
                    [make_spec(device="B")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
                )
            )
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
        accepted.extend(
            await executor.accept(
                [make_spec(device=f"D{i}")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
            )
        )
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
        (
            await executor.accept(
                [make_spec(f"s{i}", device="slow")],
                request_id=f"req-swc-{uuid4().hex}",
                caller_id="op",
            )
        )[0]
        for i in range(4)
    ]
    fast = (
        await executor.accept(
            [make_spec("f0", device="fast")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
        )
    )[0]
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
    first = await executor.accept(
        [make_spec(device="d1")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
    await asyncio.sleep(0.02)
    second = await executor.accept(
        [make_spec(device="d2")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
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
        [make_spec("p0"), make_spec("p1"), make_spec("p2")],
        request_id=f"req-swc-{uuid4().hex}",
        caller_id="op",
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
        request_id=f"req-swc-{uuid4().hex}",
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
    accepted = await executor.accept(
        [make_spec(point) for point in points], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
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
    accepted = await executor.accept(
        [make_spec(point) for point in points], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
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


async def test_serial_batch_write_timeout_stops_unstarted_suffix_and_preserves_unknown(
    control, repository, sqlite_writer, buffer, persistence
) -> None:
    """R1-F1 M-P1-01：超时后未开始后缀 NOT_SENT 且现场零写入。

    已确认前缀保留真实结果；在途未确认点 UNKNOWN；底层迟到收敛不得覆盖终态；
    后缀 p2 即使在 shield 原生任务恢复后也绝不下发（设计 §3.1.5.4、§5.3）。
    """
    session = _HangThenResumeSession()
    executor = build_executor(control, {"WT01": session}, max_batch_points=10, write_timeout=0.3)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")],
        request_id=f"req-swc-{uuid4().hex}",
        caller_id="op",
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
            "p2": CommandState.INTERRUPTED,
        }
        suffix = buffer.get_cached(accepted[2].command_id)
        assert suffix.outcome is CommandOutcome.NOT_SENT
        assert suffix.error_code == "BATCH_ABORTED_NOT_SENT"
        assert session.one_calls == ["p0", "p1"]
        session.resume.set()
        # 等原生在途写收敛并完成物理隔离（设备解除阻断），再断言后缀仍零写入。
        deadline = time.monotonic() + 3.0
        while executor.blocked_devices() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert session.one_calls == ["p0", "p1"]
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
    # 落盘后逐点终态与内存一致：前缀成功、在途 UNKNOWN、后缀 NOT_SENT。
    assert {point: r.state for point, r in durable.items()} == {
        "p0": CommandState.SUCCEEDED,
        "p1": CommandState.UNKNOWN,
        "p2": CommandState.INTERRUPTED,
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
        [make_spec("p0"), make_spec("p1"), make_spec("p2")],
        request_id=f"req-swc-{uuid4().hex}",
        caller_id="op",
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
    accepted = seed_accept(
        repo, [make_spec("p1")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
    command_id = accepted[0].command_id
    seed_transition(repo, command_id, CommandState.CONNECTING)
    if executing:
        seed_transition(repo, command_id, CommandState.EXECUTING)
    return command_id


def _seed_accepted_only(repo: EventRepository) -> str:
    return seed_accept(
        repo, [make_spec("p2")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )[0].command_id


async def test_recover_without_t2_evidence_is_conservatively_unknown(
    tmp_path, sqlite_writer
) -> None:
    """崩溃恢复保守裁决：durable EXECUTING 与仅 durable ACCEPTED 均为 UNKNOWN。

    Commander v1_22 §9.5：缓冲 T2 可能在崩溃前已触发物理 I/O，缺失持久 T2
    不证明未发送，禁止裁决 NOT_SENT；两种情况下都绝不重发现场命令
    （recover 只写终态事件）。
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
        assert accepted_result.state is CommandState.UNKNOWN
        assert accepted_result.outcome is CommandOutcome.UNKNOWN
    finally:
        sqlite_writer.submit(reopen.close).result()


async def test_crash_before_flush_loses_volatile_events_without_replay(
    tmp_path, sqlite_writer
) -> None:
    """T1 门禁下崩溃语义：未落盘即受理超时，无现场 I/O；重开后空库且绝不重发。

    Commander v1_22 RR-01/§9.5.1：accepted=true 前必须等到 T1 COMMIT。
    超长 worker 周期 + 短 accept_timeout 使持久化门禁超时，受理以
    TimeoutError 失败且不注册设备队列；模拟 SIGKILL 后 volatile 事件丢失，
    重开库为空、recover 无裁决、现场从未被写入。
    """
    db_path = tmp_path / "crash.db"
    repo = sqlite_writer.submit(lambda: EventRepository(db_path)).result()
    buffer = BufferedJournal()
    # 超长周期：受理超时前 worker 不可能完成任何落盘。
    worker = PersistenceWorker(buffer, repo, sqlite_writer, interval=60.0, batch_size=500)
    worker.start()
    physical: list[str] = []

    class CrashSession(FakeSession):
        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            physical.append(write.point)
            return await super().write_one(write)

    control = CommandControl(repo, sqlite_writer, buffer, accept_timeout=0.3)
    executor = build_executor(control, {"WT01": CrashSession()})
    try:
        with pytest.raises(TimeoutError):
            await executor.accept(
                [make_spec()], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
            )
        # 持久化门禁失败：命令从未进入设备队列，现场零写入。
        assert executor.pending_commands() == 0
        # 模拟进程被 SIGKILL：不排空、直接取消 worker，关闭仓储。
        assert worker._task is not None
        worker._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker._task
        assert buffer.pending_count > 0  # 事件全部滞留内存并随进程丢失
    finally:
        await executor.close()
        sqlite_writer.submit(repo.close).result()

    # 重启：库里无该命令；recover 返回空；现场未被任何人重发。
    reopen = sqlite_writer.submit(lambda: EventRepository(db_path)).result()
    try:
        assert sqlite_writer.submit(reopen.recover).result() == ()
        assert sqlite_writer.submit(reopen.metrics).result().accepted_total == 0
    finally:
        sqlite_writer.submit(reopen.close).result()
    assert physical == []


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


async def test_persistent_sqlite_outage_fails_admission_closed_without_field_io(
    repository, sqlite_writer
) -> None:
    """SQLite 长期不可用：持久化门禁超时拒新准入，零现场写入，内存严格有界。

    v1_22 RR-01/§9.5.1：T1 COMMIT 先于 accepted=true。append_events 永久
    失败时所有受理在 accept_timeout 处失败、不注册设备队列；保守 INTERRUPTED
    终态事件滞留内存，worker 失败对外可见，不存在无界积压。纯缓冲容量
    OverflowError 路径由 test_event_repository 容量用例独立覆盖。
    """
    fault = _fail_append_for(repository)  # 不恢复：长期不可用
    tiny = BufferedJournal(maximum_events=8)  # 极小容量，暴露任何无界增长
    worker = PersistenceWorker(tiny, repository, sqlite_writer, interval=0.02, batch_size=500)
    worker.start()
    tiny_control = CommandControl(repository, sqlite_writer, tiny, accept_timeout=0.3)
    sessions = {"WT01": FakeSession(), "WT02": FakeSession()}
    executor = build_executor(tiny_control, sessions, max_batch_points=10)
    try:
        with pytest.raises(TimeoutError):
            await executor.accept(
                [make_spec("p0", device="WT01"), make_spec("p1", device="WT02")],
                request_id=f"req-swc-{uuid4().hex}",
                caller_id="op",
            )
        # 持久化门禁失败：无队列条目、无现场会话写入。
        assert executor.pending_commands() == 0
        assert all(session.one_calls == [] for session in sessions.values())
        # 等过 worker 周期：失败对健康可见、事件原样保留。
        deadline = time.monotonic() + 1.0
        while worker.failure is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker.failure is not None
        assert fault["failed_batches"] >= 1
        bounded = tiny.pending_count
        assert 0 < bounded <= 8
        # 长期故障下后续受理同样 fail-closed，内存不增长（同 specs 被幂等门禁拦截
        # 或在持久化门禁处超时），不存在无界积压。
        with pytest.raises((TimeoutError, FileExistsError)):
            await executor.accept(
                [make_spec("p2", device="WT01")],
                request_id=f"req-swc-{uuid4().hex}",
                caller_id="op",
            )
        assert tiny.pending_count <= 8
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
            buffer.accept(
                [CommandSpec("WT01", f"p{index}", 1.0, "ref")],
                request_id=f"req-swc-{uuid4().hex}",
                caller_id="op",
            )
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
    buffer.accept(
        [CommandSpec("WT01", "p1", 1.0, "ref")], request_id=f"req-swc-{uuid4().hex}", caller_id="op"
    )
    await asyncio.sleep(0.05)  # 确保 worker 已进入 5s 合并等待。
    started = time.monotonic()
    assert await worker.stop(timeout=5.0)
    assert time.monotonic() - started < 1.0
    assert buffer.pending_count == 0
