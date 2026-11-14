"""R1-F1 Commander 控制安全专项复测（M-P1-01/02/03）。

全部用例使用真实 SQLite（共享 fixtures）+ 受控会话；执行顺序由
asyncio.Event / 受控时钟偏移决定，不依赖 sleep 碰运气。小轮询仅用于
等待"无论如何都会发生"的收敛，不断言特定调度次序。

覆盖：
  S1 串行批写：内部截止时间（超时回调延迟）、调用方/调度取消、原生异常、
     WRITE_MANY 超时、迟到返回不覆盖终态、后缀零现场写入。
  S2 停机：悬挂 FIFO 队首 + 后续排队、T1 受理与停机竞态、admission lock
     超时、停机与队列过期竞态、多设备并存。
  S3 恢复：initially_blocked 拒绝新控制；无本地 Session 的 isolate_device
     证据缺口复现（F1-GAP-01，整改前记录真实行为，非认可其安全性）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from commander.application.control import CommandControl
from commander.domain.command import CommandOutcome, CommandState
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)

from ..conftest import flush_buffer
from .conftest import FakeSession, build_executor, make_spec

pytestmark = pytest.mark.component


def _states(buffer: Any, accepted: Any) -> dict[str, CommandState]:
    return {c.spec.point_id: buffer.get_cached(c.command_id).state for c in accepted}


async def _poll(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not converge within timeout")


class GatedSession(FakeSession):
    """按点脚本化：p0 立即成功；p1 挂起直到 resume；其后点记录即可。"""

    def __init__(self, *, hang_point: str = "p1", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.hang_point = hang_point
        self.resume = asyncio.Event()
        self.hang_started = asyncio.Event()

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        self.one_calls.append(write.point)
        if write.point == self.hang_point:
            self.hang_started.set()
            await self.resume.wait()
        return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)


# ---------------------------------------------------------------- S1 M-P1-01


async def test_internal_deadline_stops_suffix_when_timeout_callback_delayed(
    control, buffer, monkeypatch
) -> None:
    """S1-2：事件循环尚未调度 wait_for 超时回调时，内部截止时间已禁止后缀。

    受控时钟 = 真实单调时钟 + 偏移。首点放行前把偏移推到写超时之外，
    _write_group 内部检查必须先于 wait_for 触发 _SerialWriteError；
    p1/p2 均为 NOT_SENT（而非 UNKNOWN），现场只写 p0。
    """
    import commander.application.queued_executor as qe

    offset = 0.0
    real_monotonic = time.monotonic
    monkeypatch.setattr(qe, "time", SimpleNamespace(monotonic=lambda: real_monotonic() + offset))

    release_first = asyncio.Event()
    first_started = asyncio.Event()

    class FirstGateSession(FakeSession):
        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            self.one_calls.append(write.point)
            if write.point == "p0":
                first_started.set()
                await release_first.wait()
            return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)

    session = FirstGateSession()
    # 真实 wait_for 超时 30s，远大于本用例耗时：若后缀被终止，只能来自内部检查。
    executor = build_executor(control, {"WT01": session}, max_batch_points=10, write_timeout=30.0)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")],
        request_id=f"req-f1-{uuid4().hex}",
        caller_id="op",
    )
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2.0)
        offset = 60.0  # 受控时钟越过 write_deadline；wait_for 真实超时远未到期
        release_first.set()
        await _poll(
            lambda: all(
                buffer.get_cached(c.command_id).state
                in {
                    CommandState.SUCCEEDED,
                    CommandState.UNKNOWN,
                    CommandState.INTERRUPTED,
                }
                for c in accepted
            )
        )
        assert _states(buffer, accepted) == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.INTERRUPTED,
            "p2": CommandState.INTERRUPTED,
        }
        for c in accepted[1:]:
            result = buffer.get_cached(c.command_id)
            assert result.outcome is CommandOutcome.NOT_SENT
            assert result.error_code == "BATCH_ABORTED_NOT_SENT"
        assert session.one_calls == ["p0"]
        # 内部 _SerialWriteError 路径保守阻断设备，物理隔离（close）后解除。
        await _poll(lambda: not executor.blocked_devices())
        assert session.closes >= 1
    finally:
        await executor.close(grace_seconds=5.0)


async def test_dispatcher_cancel_during_inflight_write_suffix_never_sent(control, buffer) -> None:
    """S1-4：执行被取消（CancelledError 分支）后在途 UNKNOWN、后缀 NOT_SENT。

    原生 I/O 迟到返回不得覆盖终态，也不得补发后缀。
    """
    session = GatedSession()
    executor = build_executor(control, {"WT01": session}, max_batch_points=10, write_timeout=30.0)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")],
        request_id=f"req-f1-{uuid4().hex}",
        caller_id="op",
    )
    try:
        await asyncio.wait_for(session.hang_started.wait(), timeout=2.0)
        worker = executor._workers["WT01"]  # noqa: SLF001
        worker.cancel()
        await _poll(
            lambda: buffer.get_cached(accepted[2].command_id).state is CommandState.INTERRUPTED
        )
        assert _states(buffer, accepted) == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.UNKNOWN,
            "p2": CommandState.INTERRUPTED,
        }
        session.resume.set()
        await _poll(lambda: not executor.blocked_devices())
        assert session.one_calls == ["p0", "p1"]
        assert _states(buffer, accepted)["p1"] is CommandState.UNKNOWN
    finally:
        await executor.close(grace_seconds=5.0)


async def test_native_write_exception_marks_current_unknown_suffix_not_sent(
    control, buffer
) -> None:
    """S1-5：原生写入异常：当前点保守 UNKNOWN，未开始后缀 NOT_SENT。"""
    session = GatedSession()

    async def fail_p1(write: ProtocolWrite) -> ProtocolWriteResult:
        session.one_calls.append(write.point)
        if write.point == "p1":
            raise ConnectionError("native link lost")
        return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)

    session.write_one = fail_p1  # type: ignore[method-assign]
    executor = build_executor(control, {"WT01": session}, max_batch_points=10, write_timeout=5.0)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")],
        request_id=f"req-f1-{uuid4().hex}",
        caller_id="op",
    )
    try:
        await _poll(
            lambda: buffer.get_cached(accepted[2].command_id).state is CommandState.INTERRUPTED
        )
        assert _states(buffer, accepted) == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.UNKNOWN,
            "p2": CommandState.INTERRUPTED,
        }
        current = buffer.get_cached(accepted[1].command_id)
        assert current.outcome is CommandOutcome.UNKNOWN
        assert current.error_code == "ConnectionError"
        assert buffer.get_cached(accepted[2].command_id).outcome is CommandOutcome.NOT_SENT
        assert session.one_calls == ["p0", "p1"]
    finally:
        await executor.close(grace_seconds=5.0)


async def test_write_many_timeout_marks_whole_batch_unknown_without_resend(control, buffer) -> None:
    """S1-6：WRITE_MANY 单原生调用超时：整批 UNKNOWN，不存在 NOT_SENT 后缀。

    批调用可能已部分生效，不能证明任何点未发送；迟到返回不得覆盖终态，
    且 write_many 只调用一次（不自动重发）。
    """

    class ManyHangSession(FakeSession):
        def __init__(self) -> None:
            super().__init__(
                capabilities=frozenset(
                    {
                        ProtocolCapability.READ,
                        ProtocolCapability.WRITE,
                        ProtocolCapability.WRITE_MANY,
                    }
                )
            )
            self.resume = asyncio.Event()
            self.started = asyncio.Event()

        async def write_many(self, writes):  # noqa: ANN001, ANN202
            points = tuple(w.point for w in writes)
            self.many_calls.append(points)
            self.started.set()
            await self.resume.wait()
            return tuple(ProtocolWriteResult(p, WriteOutcome.CONFIRMED_SUCCESS) for p in points)

    session = ManyHangSession()
    executor = build_executor(control, {"WT01": session}, max_batch_points=10, write_timeout=0.2)
    accepted = await executor.accept(
        [make_spec("p0"), make_spec("p1"), make_spec("p2")],
        request_id=f"req-f1-{uuid4().hex}",
        caller_id="op",
    )
    try:
        await asyncio.wait_for(session.started.wait(), timeout=2.0)
        await _poll(
            lambda: all(
                buffer.get_cached(c.command_id).state is CommandState.UNKNOWN for c in accepted
            )
        )
        session.resume.set()
        await _poll(lambda: not executor.blocked_devices())
        assert len(session.many_calls) == 1
        assert session.one_calls == []
        assert all(buffer.get_cached(c.command_id).state is CommandState.UNKNOWN for c in accepted)
    finally:
        await executor.close(grace_seconds=5.0)


# ---------------------------------------------------------------- S2 M-P1-02


async def test_close_with_hung_fifo_head_terminalizes_all_queued_followers(
    control, buffer, repository, sqlite_writer, persistence
) -> None:
    """S2-1/S2-6：悬挂队首不得使后续未开始命令永久 ACCEPTED。

    后续命令（不同 request_id、跨设备）全部 INTERRUPTED/NOT_SENT；
    停机显式报告残留阻断设备；在途 I/O 所有权不被逻辑终态提前释放。
    """
    session = GatedSession(hang_point="head")
    other = FakeSession()
    executor = build_executor(
        control,
        {"WT01": session, "WT02": other},
        max_batch_points=1,
        write_timeout=30.0,
        global_capacity=4,
    )
    head = await executor.accept(
        [make_spec("head")], request_id=f"req-f1-{uuid4().hex}", caller_id="op"
    )
    await asyncio.wait_for(session.hang_started.wait(), timeout=2.0)
    followers = []
    for point in ("f1", "f2"):
        followers += await executor.accept(
            [make_spec(point)], request_id=f"req-f1-{uuid4().hex}", caller_id="op"
        )
    # 另一台正常设备的命令在停机前正常完成，证明跨设备不受悬挂队首牵连。
    normal = await executor.accept(
        [make_spec("ok", device="WT02")], request_id=f"req-f1-{uuid4().hex}", caller_id="op"
    )
    await _poll(lambda: buffer.get_cached(normal[0].command_id).state is CommandState.SUCCEEDED)

    residual = await executor.close(grace_seconds=0.2)
    assert "WT01" in residual  # 停机失败显式报告，不伪装 STOPPED
    for c in followers:
        result = buffer.get_cached(c.command_id)
        assert result.state is CommandState.INTERRUPTED
        assert result.outcome is CommandOutcome.NOT_SENT
        assert result.error_code == "SERVICE_STOPPING"
    assert buffer.get_cached(normal[0].command_id).state is CommandState.SUCCEEDED
    # 队首原生 I/O 仍在途：不得被伪造成已终结（UNKNOWN 只能由写超时/恢复裁决）。
    head_result = buffer.get_cached(head[0].command_id)
    assert head_result.state in {CommandState.ACCEPTED, CommandState.EXECUTING}
    assert session.open  # 未安全收敛的 I/O 不释放会话所有权
    # 落盘后终态事件不重复：每个 follower 恰好一条 COMPLETED。
    await flush_buffer(buffer, persistence)
    events = sqlite_writer.submit(repository.pending_events, 1000).result()
    completed = [
        e
        for e in events
        if e.get("event_type") == "COMPLETED"
        and e.get("command_id") in {c.command_id for c in followers}
    ]
    assert len(completed) == len(followers)
    session.resume.set()


async def test_close_during_durable_admission_terminalizes_accepted_commands(
    repository, sqlite_writer, buffer, persistence
) -> None:
    """S2-2：T1 受理挂起期间开始停机：T1 落定后立即终结，绝不入队执行。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedControl(CommandControl):
        async def accept(self, commands, *, caller_id, **kwargs):  # noqa: ANN001, ANN202
            entered.set()
            await release.wait()
            return await super().accept(commands, caller_id=caller_id, **kwargs)

    gated = GatedControl(repository, sqlite_writer, buffer)
    session = FakeSession()
    executor = build_executor(gated, {"WT01": session})
    accept_task = asyncio.create_task(
        executor.accept([make_spec("p0")], request_id=f"req-f1-{uuid4().hex}", caller_id="op")
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    close_task = asyncio.create_task(executor.close(grace_seconds=0.2))
    # 停机先开始（admission lock 被受理持有 → close 记录 __admission__ 失败标记）。
    residual = await asyncio.wait_for(close_task, timeout=3.0)
    assert "__admission__" in residual
    release.set()
    accepted = await asyncio.wait_for(accept_task, timeout=2.0)
    result = buffer.get_cached(accepted[0].command_id)
    assert result.state is CommandState.INTERRUPTED
    assert result.outcome is CommandOutcome.NOT_SENT
    assert result.error_code == "SERVICE_STOPPING"
    assert session.one_calls == []
    assert executor.pending_commands() == 0


async def test_close_admission_lock_timeout_still_terminalizes_queue_and_reports_failure(
    control, buffer
) -> None:
    """S2-3：admission lock 超时：仍终结排队命令，返回值显式携带 __admission__。"""
    session = GatedSession(hang_point="head")
    executor = build_executor(control, {"WT01": session}, max_batch_points=1, write_timeout=30.0)
    await executor.accept([make_spec("head")], request_id=f"req-f1-{uuid4().hex}", caller_id="op")
    await asyncio.wait_for(session.hang_started.wait(), timeout=2.0)
    follower = await executor.accept(
        [make_spec("f1")], request_id=f"req-f1-{uuid4().hex}", caller_id="op"
    )
    # 人为持有受理锁，模拟另一条受理路径在停机期间迟迟不释放。
    await executor._admission_lock.acquire()  # noqa: SLF001
    try:
        residual = await executor.close(grace_seconds=0.2)
    finally:
        executor._admission_lock.release()  # noqa: SLF001
    assert "__admission__" in residual
    assert "WT01" in residual
    result = buffer.get_cached(follower[0].command_id)
    assert result.state is CommandState.INTERRUPTED
    assert result.outcome is CommandOutcome.NOT_SENT
    session.resume.set()


async def test_close_racing_queue_expiry_produces_exactly_one_terminal_outcome(
    control, buffer, monkeypatch
) -> None:
    """S2-4/S2-5：停机与队列过期/等待取消竞态：终态唯一，不重复产生终态事件。

    两种合法终态（EXPIRED 或 INTERRUPTED）均可接受，但 control.complete
    对同一命令的终态转移必须恰好一次。
    """
    session = GatedSession(hang_point="head")
    executor = build_executor(
        control,
        {"WT01": session},
        max_batch_points=1,
        write_timeout=30.0,
        queue_timeout=0.15,
    )
    terminal_calls: list[str] = []
    original = control.complete

    async def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        terminal_calls.append(args[0])
        return await original(*args, **kwargs)

    monkeypatch.setattr(control, "complete", spy)
    await executor.accept([make_spec("head")], request_id=f"req-f1-{uuid4().hex}", caller_id="op")
    await asyncio.wait_for(session.hang_started.wait(), timeout=2.0)
    follower = await executor.accept(
        [make_spec("f1")], request_id=f"req-f1-{uuid4().hex}", caller_id="op"
    )
    waiter = asyncio.create_task(executor.wait(follower[0].command_id))
    await asyncio.sleep(0.05)  # 让过期与停机窗口重叠；两种次序都合法
    residual = await executor.close(grace_seconds=0.5)
    assert "WT01" in residual
    await asyncio.wait_for(waiter, timeout=2.0)
    result = buffer.get_cached(follower[0].command_id)
    assert result.state in {CommandState.EXPIRED, CommandState.INTERRUPTED}
    assert result.outcome is CommandOutcome.NOT_SENT
    assert terminal_calls.count(follower[0].command_id) == 1
    session.resume.set()


# ---------------------------------------------------------------- S3 M-P1-03


async def test_recovered_blocked_device_rejects_new_control_at_admission(control) -> None:
    """S3：恢复的不确定设备在受理阶段即拒绝新冲突命令，不产生任何现场 I/O。"""
    session = FakeSession()
    executor = build_executor(
        control, {"WT01": session, "WT02": FakeSession()}, initially_blocked=frozenset({"WT01"})
    )
    with pytest.raises(RuntimeError, match="device unavailable"):
        await executor.accept([make_spec("p0")], request_id=f"req-f1-{uuid4().hex}", caller_id="op")
    assert session.one_calls == [] and session.many_calls == []
    # 恢复设备与其他正常设备并存：阻断集合精确到设备，不波及其他设备。
    assert executor.blocked_devices() == frozenset({"WT01"})
    await executor.close(grace_seconds=1.0)


async def test_isolate_without_local_session_clears_block_without_physical_evidence(
    control,
) -> None:
    """S3-5 F1-GAP-01（已知缺口复现，非安全认可）：无本地 Session 时
    isolate_device 直接清除 BLOCKED，全程没有任何物理隔离证据。

    重启后的恢复设备在当前进程没有旧 Session；该路径使"未确认隔离"的
    设备可以在零证据下重新开放控制。整改后本用例应改为：无证据时拒绝
    解除阻断（返回 False 或要求显式安全证据），不得保留现行为。
    """
    session = FakeSession()
    executor = build_executor(control, {"WT01": session}, initially_blocked=frozenset({"WT01"}))
    assert executor._sessions == {}  # noqa: SLF001  # 旧进程 I/O 无本地句柄
    cleared = await executor.isolate_device("WT01")
    # 当前真实行为：无 Session 即认为可解除，无任何物理证据。
    assert cleared is True
    assert executor.blocked_devices() == frozenset()
    assert session.closes == 0  # 未发生任何物理关闭/隔离动作
    await executor.close(grace_seconds=1.0)
