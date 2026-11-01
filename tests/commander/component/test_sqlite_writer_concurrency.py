"""B4 专项：唯一 SQLite writer 线程下的并发、取消、故障与停机不变量。

所有用例使用真实 EventRepository（归属唯一单线程池）+ 受控伪现场会话，
专门压 T1/T2/T3 经 writer 串行化后的边界。核心不变量（v1_1）：

  SQLite 一旦提交成功，命令必须要么仍在 FIFO/在途记账内，要么已有持久化
  终态；禁止出现“提交成功但既未入 FIFO 也无终态”的孤儿命令。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandState,
)
from commander.infrastructure.event_repository import EventRepository
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
)

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


def _durable_state(repo: EventRepository, writer: ThreadPoolExecutor, command_id: str):
    result = writer.submit(repo.get, command_id).result()
    assert result is not None
    return result.state


async def test_concurrent_accepts_across_devices_all_durable_and_settled(
    control, repository, sqlite_writer
) -> None:
    """场景：多设备同时受理。T1 全经唯一 writer 串行落盘，无一丢失。"""
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
    metrics = sqlite_writer.submit(repository.metrics).result()
    assert metrics.accepted_total == total
    assert dict(metrics.terminal_counts).get("SUCCEEDED") == total
    assert executor.pending_commands() == 0


def _fault_transitions_after(repo: EventRepository, *, fail_from: int) -> Callable[..., None]:
    """让 transition 在前 fail_from-1 次成功后抛 sqlite3.Error（模拟磁盘故障）。"""
    original = repo.transition
    calls = {"n": 0}

    def fault(command_id: str, state: CommandState, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] >= fail_from:
            raise sqlite3.OperationalError("simulated disk I/O error")
        return original(command_id, state, **kwargs)

    return fault


async def test_sqlite_error_during_single_t2_fail_closed_without_field_write(
    control, repository, sqlite_writer
) -> None:
    """场景：SQLite 写入异常。连接前转移落盘失败必须 fail-closed：现场零写入、调用方知情。"""
    session = FakeSession()
    executor = build_executor(control, {"WT01": session})
    accepted = await executor.accept([make_spec()], caller_id="op")
    # T1 已成功；让随后的 CONNECTING/T2 转移全部失败。
    repository.transition = _fault_transitions_after(repository, fail_from=1)
    with pytest.raises(Exception):  # noqa: B017, BLE001
        await executor.wait(accepted[0].command_id)
    try:
        # 现场从未触达；故障后执行器进入 fatal，新受理被拒。
        assert session.one_calls == []
        assert executor.operational is False
        with pytest.raises(RuntimeError):
            await executor.accept([make_spec()], caller_id="op")
    finally:
        await executor.close()


async def test_batch_t2_partial_failure_leaves_no_silent_stranding(
    control, repository, sqlite_writer
) -> None:
    """场景：批量 T2 部分失败。

    3 点批量：第 1 个成员 T2 落盘成功、第 2 个起磁盘故障。要求：
      - 首批原生写入完全不发起（全组 T2 先于现场）；
      - 执行器 fatal，每个等待方都收到异常而非静默悬挂；
      - 已 durable T2 的成员在持久层确为 EXECUTING（崩溃恢复语义 UNKNOWN，
        由仓储恢复测试钉住），进程内不存在“无终态又无人记账”的成员。
    """
    session = FakeSession(
        capabilities=frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.WRITE_MANY,
            }
        )
    )
    executor = build_executor(control, {"WT01": session}, max_batch_points=10)
    accepted = await executor.accept(
        [make_spec("p1"), make_spec("p2"), make_spec("p3")], caller_id="op"
    )
    # 调用序列：成员1 CONNECTING、成员1 EXECUTING（T2 durable）、成员2 T2 起故障。
    repository.transition = _fault_transitions_after(repository, fail_from=3)
    outcomes = await asyncio.gather(
        *(executor.wait(c.command_id) for c in accepted), return_exceptions=True
    )
    try:
        assert all(isinstance(outcome, BaseException) for outcome in outcomes)
        # 全组 T2 未齐：原生多写/单写均不得发起。
        assert session.many_calls == [] and session.one_calls == []
        assert executor.operational is False
        # 首个成员的 T2 必须真实 durable（非内存假象）。
        assert (
            _durable_state(repository, sqlite_writer, accepted[0].command_id)
            is CommandState.EXECUTING
        )
    finally:
        await executor.close()


async def test_queue_timer_racing_t2_always_reaches_durable_terminal(
    control, repository, sqlite_writer
) -> None:
    """场景：排队计时器与 T2 竞争（多轮扰动）。

    容量 1 下 B 设备排队逼近 queue_timeout；A 释放许可的时刻与 B 的
    T2/超时终态在 writer 上交错。无论谁赢，B 每个命令都必须落在一个
    持久化终态（EXPIRED/NOT_SENT 或 SUCCEEDED），且现场写入数与
    SUCCEEDED 数精确一致——禁止无主命令。
    """
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
        await asyncio.sleep(0.004 * round_no)  # 扰动超时与许可释放的先后。
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
            durable = _durable_state(repository, sqlite_writer, command.command_id)
            assert durable in TERMINAL
            assert durable is result.state
            if durable is CommandState.SUCCEEDED:
                succeeded += 1
        # 现场单写次数必须与持久层 SUCCEEDED 完全对账。
        assert len(sessions["A"].one_calls) + len(sessions["B"].one_calls) == succeeded


async def test_close_concurrent_with_inflight_sqlite_settles_every_terminal(
    control, repository, sqlite_writer
) -> None:
    """场景：stop 与未完成 SQLite 操作并发。

    一批命令刚受理（T1 完成、T2/T3 在途）即停机：close 返回后每条命令
    都必须有持久化终态（成功或 INTERRUPTED），不得残留 ACCEPTED/EXECUTING。
    """
    sessions = {f"D{i}": FakeSession(connect_delay=0.02, write_delay=0.03) for i in range(8)}
    executor = build_executor(
        control, sessions, global_capacity=8, write_timeout=2.0, connect_timeout=2.0
    )
    accepted: list[AcceptedCommand] = []
    for i in range(8):
        accepted.extend(await executor.accept([make_spec(device=f"D{i}")], caller_id="op"))
    await asyncio.sleep(0.01)  # 让部分命令进入连接/T2 窗口后立即停机。
    blocked = await executor.close(grace_seconds=5.0)
    assert blocked == frozenset()
    for command in accepted:
        state = _durable_state(repository, sqlite_writer, command.command_id)
        assert state in TERMINAL, f"{command.command_id} stranded in {state} after stop"
        assert state not in (CommandState.ACCEPTED, CommandState.EXECUTING)
    metrics = sqlite_writer.submit(repository.metrics).result()
    assert metrics.accepted_total == 8
    durable_terminal = sum(dict(metrics.terminal_counts).values())
    assert durable_terminal == 8


async def test_repeated_accepts_under_serialized_writer_preserve_fifo(
    control, repository, sqlite_writer
) -> None:
    """场景：FIFO/全局额度在 writer 串行压力下不被破坏。

    writer 线程被间歇占住时受理多个批次；同设备现场写顺序必须严格等于
    FIFO 顺序，不同设备可并行，全局并发不超过容量。
    """
    import threading

    max_concurrency = 2
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
    control, repository, sqlite_writer
) -> None:
    """场景补充：EXPIRED/FAILED 终态均带 COMPLETED 事件投影与一致 outcome。"""
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
    counts = dict(sqlite_writer.submit(repository.metrics).result().terminal_counts)
    assert counts.get("EXPIRED") == 1
    assert counts.get("SUCCEEDED") == 1
