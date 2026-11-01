"""QueuedExecutor 组件测试：真实 SQLite 决策落盘 + 受控伪会话现场 I/O。

覆盖：FIFO、全局门、批量合并、排队超时、写前过期、连接失败、写超时 UNKNOWN、
设备拒绝、取消、幂等重放、阻塞隔离、关闭边界。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from commander.application.queued_executor import QueuedExecutor
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandState,
)
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)

from .conftest import FakeSession, build_executor, make_spec, wait_for_result

pytestmark = pytest.mark.component


async def _drain(
    executor: QueuedExecutor,
    accepted: tuple[AcceptedCommand, ...],
    *,
    timeout: float = 2.0,
) -> list[CommandResult]:
    return [await wait_for_result(executor, command, timeout=timeout) for command in accepted]


async def test_happy_path_persists_all_boundaries_and_writes_one(control) -> None:
    session = FakeSession()
    executor = build_executor(control, {"WT01": session})
    accepted = executor.accept([make_spec()], caller_id="op-1")
    try:
        results = await _drain(executor, accepted)
    finally:
        assert await executor.close() == frozenset()
    assert results[0].state is CommandState.SUCCEEDED
    assert results[0].outcome is CommandOutcome.CONFIRMED_SUCCESS
    assert session.connects == 1
    assert session.one_calls == ["setpoint"]
    assert session.closes == 1


async def test_fifo_order_within_one_device_and_parallel_devices(control) -> None:
    """同设备严格 FIFO；不同设备可并行，慢设备不阻塞快设备。"""
    order: list[str] = []

    class OrderSession(FakeSession):
        def __init__(self, tag: str, delay: float) -> None:
            super().__init__(write_delay=delay)
            self._tag = tag

        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            order.append(f"start:{self._tag}:{write.point}")
            try:
                return await super().write_one(write)
            finally:
                order.append(f"end:{self._tag}:{write.point}")

    sessions = {"slow": OrderSession("slow", 0.15), "fast": OrderSession("fast", 0.0)}
    executor = build_executor(control, sessions, global_capacity=8)
    a = executor.accept([make_spec("p1", device="slow")], caller_id="op")
    b = executor.accept([make_spec("p2", device="slow")], caller_id="op")
    accept_fast = executor.accept([make_spec("p3", device="fast")], caller_id="op")
    try:
        fast_result = await wait_for_result(executor, accept_fast[0], timeout=1.0)
        # fast 在 slow 第一拍尚未结束时完成：设备间未被 FIFO 串行化。
        assert fast_result.state is CommandState.SUCCEEDED
        assert order[0].startswith("start:slow:p1")
        assert order[1] == "start:fast:p3"
        results = await _drain(executor, a + b)
        assert [r.state for r in results] == [CommandState.SUCCEEDED, CommandState.SUCCEEDED]
        # slow 内部 p1 完全结束后 p2 才开始。
        assert order.index("end:slow:p1") < order.index("start:slow:p2")
    finally:
        await executor.close()


async def test_batch_points_coalesce_into_single_write_many(control) -> None:
    session = FakeSession(
        capabilities=frozenset(
            {ProtocolCapability.READ, ProtocolCapability.WRITE, ProtocolCapability.WRITE_MANY}
        )
    )
    executor = build_executor(control, {"WT01": session}, max_batch_points=10)
    # 同一次请求的多个点必须合并为一次原生多写。
    accepted = executor.accept([make_spec("p1"), make_spec("p2"), make_spec("p3")], caller_id="op")
    try:
        results = await _drain(executor, accepted)
    finally:
        await executor.close()
    assert session.many_calls == [("p1", "p2", "p3")]
    assert session.one_calls == []
    assert all(r.state is CommandState.SUCCEEDED for r in results)


async def test_driver_without_write_many_falls_back_to_write_one(control) -> None:
    # Modbus 驱动只声明 WRITE：合并必须被能力门禁用。
    session = FakeSession()  # 默认无 WRITE_MANY
    executor = build_executor(control, {"WT01": session})
    accepted = executor.accept([make_spec("p1"), make_spec("p2")], caller_id="op")
    try:
        await _drain(executor, accepted)
    finally:
        await executor.close()
    assert session.one_calls == ["p1", "p2"]
    assert session.many_calls == []


async def test_batch_over_limit_rejected_before_persistence(control, repository) -> None:
    session = FakeSession()
    executor = build_executor(control, {"WT01": session}, max_batch_points=2)
    with pytest.raises(OverflowError):
        executor.accept([make_spec("p1"), make_spec("p2"), make_spec("p3")], caller_id="op")
    await executor.close()
    assert repository.metrics().accepted_total == 0


async def test_queue_timeout_expires_waiter_without_field_write(control) -> None:
    gate_blocker = asyncio.Event()

    class HangingSession(FakeSession):
        async def write_one(self, write):
            await gate_blocker.wait()
            return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)

    sessions = {"d1": HangingSession(), "d2": FakeSession()}
    executor = build_executor(
        control,
        sessions,
        global_capacity=1,
        queue_timeout=0.1,
        connect_timeout=1.0,
        write_timeout=5.0,
    )
    executor.accept([make_spec("p1", device="d1")], caller_id="op")
    # 让第一拍拿到唯一许可并挂在原生写中。
    await asyncio.sleep(0.05)
    second = executor.accept([make_spec("p2", device="d2")], caller_id="op")
    try:
        result = await wait_for_result(executor, second[0], timeout=2.0)
        assert result.state is CommandState.EXPIRED
        assert result.outcome is CommandOutcome.NOT_SENT
        assert result.error_code == "QUEUE_TIMEOUT"
        assert sessions["d2"].connects == 0
    finally:
        gate_blocker.set()
        blocked = await executor.close(grace_seconds=3.0)
        # d1 挂起写入在关闭窗口内完成则正常关闭；超时则如实报告 blocked。
        assert isinstance(blocked, frozenset)


async def test_expired_before_t2_when_connect_outlasts_deadline(control) -> None:
    future = datetime.now(UTC) + timedelta(milliseconds=200)
    session = FakeSession(connect_delay=0.4)
    executor = build_executor(control, {"WT01": session}, connect_timeout=2.0, write_timeout=2.0)
    spec = make_spec()
    object.__setattr__(spec, "expires_at", future)
    accepted = executor.accept([spec], caller_id="op")
    try:
        result = await wait_for_result(executor, accepted[0], timeout=3.0)
    finally:
        await executor.close()
    assert result.state is CommandState.EXPIRED
    assert result.outcome is CommandOutcome.NOT_SENT
    assert result.error_code == "EXPIRED"
    # 绝不允许在过期后落 T2/发生现场写。
    assert session.one_calls == []


async def test_connect_failure_confirmed_not_sent_and_session_removed(control) -> None:
    session = FakeSession(connect_error=ConnectionRefusedError("unreachable"))
    executor = build_executor(control, {"WT01": session})
    accepted = executor.accept([make_spec()], caller_id="op")
    try:
        result = await _drain(executor, accepted)
        assert result[0].state is CommandState.FAILED
        assert result[0].error_code == "CONNECT_FAILED"
        # 连接失败后会话被关闭移除；后续命令重建会话再试。
        session._connect_error = None
        second = executor.accept([make_spec()], caller_id="op")
        results2 = await _drain(executor, second)
        assert results2[0].state is CommandState.SUCCEEDED
        assert session.connects == 2
    finally:
        await executor.close()


async def test_write_timeout_marks_unknown_blocks_device_and_retains_permit(
    control,
) -> None:
    session = FakeSession(write_delay=0.3)
    executor = build_executor(
        control,
        {"WT01": session},
        global_capacity=2,
        connect_timeout=1.0,
        write_timeout=0.05,
    )
    accepted = executor.accept([make_spec()], caller_id="op")
    result = await wait_for_result(executor, accepted[0], timeout=2.0)
    assert result.state is CommandState.UNKNOWN
    assert result.outcome is CommandOutcome.UNKNOWN
    assert result.error_code == "WRITE_TIMEOUT"
    assert "WT01" in executor.blocked_devices()
    assert executor.in_flight_native() >= 1
    # 阻塞期间新命令 fail-fast，不得进入现场。
    with pytest.raises(RuntimeError):
        executor.accept([make_spec()], caller_id="op")
    # 原生任务结束 + 物理 close 成功后自动隔离：解除阻塞、释放许可。
    await asyncio.sleep(0.5)
    assert executor.blocked_devices() == frozenset()
    assert executor.in_flight_native() == 0
    assert session.closes >= 1
    blocked = await executor.close()
    assert blocked == frozenset()


async def test_write_exception_blocks_device_and_unknown_outcome(control) -> None:
    # 物理 close 同样失败时，设备必须保持阻塞，许可不得归还。
    session = FakeSession(write_error=OSError("link reset"), close_error=OSError("socket gone"))
    executor = build_executor(control, {"WT01": session}, write_timeout=1.0, connect_timeout=1.0)
    accepted = executor.accept([make_spec()], caller_id="op")
    result = await wait_for_result(executor, accepted[0], timeout=2.0)
    assert result.state is CommandState.UNKNOWN
    assert result.error_code == "OSError"
    await asyncio.sleep(0.05)  # 等自动隔离尝试落定。
    assert "WT01" in executor.blocked_devices()
    assert executor.in_flight_native() >= 1
    blocked = await executor.close()
    assert "WT01" in blocked


async def test_device_rejection_is_confirmed_failure_not_unknown(control) -> None:
    session = FakeSession(outcome=WriteOutcome.CONFIRMED_FAILURE)
    executor = build_executor(control, {"WT01": session})
    accepted = executor.accept([make_spec()], caller_id="op")
    try:
        result = (await _drain(executor, accepted))[0]
    finally:
        await executor.close()
    assert result.state is CommandState.FAILED
    assert result.outcome is CommandOutcome.CONFIRMED_FAILURE
    assert result.error_code == "DEVICE_REJECTED"
    # 明确拒绝不阻塞设备。
    assert executor.blocked_devices() == frozenset()


async def test_cancel_queued_command_is_not_sent(control) -> None:
    gate = asyncio.Event()

    class OrderedSession(FakeSession):
        async def write_one(self, write):
            gate.set()
            return await super().write_one(write)

    sessions = {"WT01": OrderedSession()}
    executor = build_executor(control, sessions, global_capacity=1)
    first = executor.accept([make_spec("p1")], caller_id="op")
    await gate.wait()  # 等第一拍开始。
    second = executor.accept([make_spec("p2")], caller_id="op")
    assert executor.cancel(second[0].command_id) is True
    try:
        result = await wait_for_result(executor, second[0], timeout=2.0)
        assert result.state is CommandState.CANCELLED
        assert result.outcome is CommandOutcome.NOT_SENT
        await _drain(executor, first)
        assert sessions["WT01"].one_calls == ["p1"]
    finally:
        await executor.close()


async def test_idempotent_replay_never_writes_twice(control) -> None:
    session = FakeSession()
    executor = build_executor(control, {"WT01": session})
    specs = [make_spec()]
    first = executor.accept(specs, caller_id="op", idempotency_key="dedup-1")
    replay = executor.accept(specs, caller_id="op", idempotency_key="dedup-1")
    try:
        await _drain(executor, first)
    finally:
        await executor.close()
    assert tuple(c.command_id for c in replay) == tuple(c.command_id for c in first)
    assert session.one_calls == ["setpoint"]


async def test_close_keeps_hanging_native_workers_blocked(control) -> None:
    gate = asyncio.Event()

    class HangingSession(FakeSession):
        async def write_one(self, write):
            await gate.wait()
            return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)

    session = HangingSession()
    executor = build_executor(
        control,
        {"WT01": session},
        global_capacity=2,
        write_timeout=30.0,
        connect_timeout=1.0,
    )
    executor.accept([make_spec()], caller_id="op")
    await asyncio.sleep(0.05)
    blocked = await executor.close(grace_seconds=0.1)
    assert "WT01" in blocked
    assert executor.operational is False
    gate.set()
    await asyncio.sleep(0.05)


async def test_accept_after_close_is_rejected(control) -> None:
    session = FakeSession()
    executor = build_executor(control, {"WT01": session})
    await executor.close()
    with pytest.raises(RuntimeError):
        executor.accept([make_spec()], caller_id="op")


async def test_connect_timeout_is_failed_not_unknown(control) -> None:
    release = asyncio.Event()

    class SlowConnect(FakeSession):
        async def connect(self) -> None:
            self.connects += 1
            await release.wait()

    session = SlowConnect()
    executor = build_executor(control, {"WT01": session}, connect_timeout=0.05, write_timeout=1.0)
    accepted = executor.accept([make_spec()], caller_id="op")
    result = await wait_for_result(executor, accepted[0], timeout=2.0)
    # 连接超时现场未写：可确证 NOT_SENT 语义走 FAILED/CONNECT_TIMEOUT。
    assert result.state is CommandState.FAILED
    assert result.error_code == "CONNECT_TIMEOUT"
    blocked = await executor.close(grace_seconds=0.2)
    assert "WT01" in blocked
    release.set()
    await asyncio.sleep(0.02)


async def test_quiesce_drains_device_then_rejects_with_reloading_error(control) -> None:
    gate = asyncio.Event()

    class SerialSession(FakeSession):
        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            gate.set()
            await asyncio.sleep(0.1)
            return await super().write_one(write)

    session = SerialSession()
    executor = build_executor(control, {"WT01": session}, write_timeout=2.0, connect_timeout=1.0)
    first = executor.accept([make_spec("p1")], caller_id="op")
    await gate.wait()
    quiesce = asyncio.create_task(executor.quiesce_devices(frozenset({"WT01"})))
    await asyncio.sleep(0.05)
    # quiesce 期间新受理直接失败（fail-fast，不入队）。
    with pytest.raises(RuntimeError):
        executor.accept([make_spec("p2")], caller_id="op")
    assert await asyncio.wait_for(quiesce, timeout=2.0) == frozenset()
    result = await wait_for_result(executor, first[0], timeout=2.0)
    assert result.state is CommandState.SUCCEEDED
    # quiesce 返回后设备可重新受理（配合 reload 热替换场景）。
    again = executor.accept([make_spec("p3")], caller_id="op")
    results = await _drain(executor, again)
    assert results[0].state is CommandState.SUCCEEDED
    await executor.close()
