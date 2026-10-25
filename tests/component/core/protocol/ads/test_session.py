"""_AdsSession：单锁串行化、取消安全与一次性资源释放。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.application.errors import ProtocolError
from core.infrastructure.protocol.ads.session import (
    ADSCommandNotSentError,
    ADSResourceUncertainError,
    _AdsSession,
)
from tests.support.ads_fake import FakePyadsConnection, Gate


def _session(connection: FakePyadsConnection | None = None) -> _AdsSession:
    connection = connection or FakePyadsConnection()
    return _AdsSession(lambda: connection)


async def test_open_is_one_shot_and_enables_commands() -> None:
    session = _session()
    await session.open()
    assert session.is_open
    with pytest.raises(ProtocolError):
        await session.open()
    assert (await session.execute("read_state")) == (5, 0)


async def test_execute_rejected_before_open() -> None:
    session = _session()
    with pytest.raises(ADSCommandNotSentError):
        await session.execute("read_state")


async def test_physical_commands_never_overlap_single_lock() -> None:
    connection = FakePyadsConnection()
    session = _session(connection)
    await session.open()

    in_flight = 0
    peak = 0
    lock = threading.Lock()
    first = Gate()

    def read_state() -> tuple[int, int]:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            first.wait()
            return (5, 0)
        finally:
            with lock:
                in_flight -= 1

    def set_timeout(ms: int) -> None:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                connection.timeouts.append(ms)
            finally:
                in_flight -= 1

    connection.read_state = read_state  # type: ignore[method-assign]
    connection.set_timeout = set_timeout  # type: ignore[method-assign]

    running = asyncio.create_task(session.execute("read_state"))
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(session.execute("set_timeout", 100))
    await asyncio.sleep(0.05)
    assert first.waiters == 1
    first.open()
    await running
    await queued
    assert peak == 1
    assert connection.timeouts == [100]


async def test_queued_command_cancelled_before_dispatch_is_never_sent() -> None:
    connection = FakePyadsConnection()
    session = _session(connection)
    await session.open()
    block = Gate()
    connection.block_before["read_state"] = block

    occupying = asyncio.create_task(session.execute("read_state"))
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(session.execute("read_state"))
    await asyncio.sleep(0.05)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    block.open()
    await occupying
    # 让被取消的排队 owner task 落地，然后验证连接仍可用且只下发两次占用中的一次。
    await asyncio.sleep(0.05)
    connection.block_before.clear()
    assert (await session.execute("read_state")) == (5, 0)
    assert [name for name, _ in connection.calls if name == "read_state"] == [
        "read_state",
        "read_state",
    ]


async def test_running_command_survives_outer_timeout_and_close_waits() -> None:
    """wait_for 超时只取消等待方；已提交线程必须跑完，close 持锁等待。"""
    connection = FakePyadsConnection()
    session = _session(connection)
    await session.open()
    gate = Gate()
    connection.block_before["read_write"] = gate

    timed_out = asyncio.create_task(
        asyncio.wait_for(session.execute("read_write", 0, 1, None, None, None), timeout=0.1)
    )
    await asyncio.sleep(0.2)
    with pytest.raises(TimeoutError):
        await timed_out
    assert gate.waiters == 1  # 物理调用仍在线程里被阻塞

    closed = asyncio.create_task(session.close())
    await asyncio.sleep(0.1)
    assert not closed.done()  # close 必须等持门物理操作结束
    gate.open()
    await asyncio.wait_for(closed, timeout=2)
    assert session.is_settled
    assert connection.closed_calls == 1


async def test_confirmed_open_failure_is_settled_without_physical_close() -> None:
    # pyads 3.6.0：同步 open() 抛错时内部已回滚本地端口。这是明确结果，
    # 不等同于取消导致的物理状态不明：close 不得再升级为 uncertain，
    # 否则 route_auto_add 的“注册路由后重试一次”路径永远不可达（D1/D2）。
    connection = FakePyadsConnection()
    connection.fail["open"] = RuntimeError("router refused")
    session = _session(connection)
    with pytest.raises(RuntimeError):
        await session.open()
    assert session.open_failed
    assert not session.is_uncertain
    assert not session.is_open
    await session.close()
    assert session.is_settled
    # 未真正打开：物理 close 不应再下发（pyads 已自行回滚）。
    assert connection.closed_calls == 0


async def test_cancelled_queued_open_is_certain_noop() -> None:
    # 物理 open 获得门之前被取消：命令未分发，pyads 侧不可能已动作，
    # 属确定的未发送，close 可正常收敛（无物理调用、无残留）。
    connection = FakePyadsConnection()
    session = _session(connection)
    await session._gate.acquire()
    opening = asyncio.create_task(session.open())
    await asyncio.sleep(0.05)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    session._gate.release()
    assert not session.is_uncertain
    assert not session.open_failed
    assert [name for name, _ in connection.calls] == []
    await session.close()
    assert session.is_settled


async def test_physical_open_base_failure_is_uncertain_and_fail_closed() -> None:
    # 物理 open 已分发但抛出 Exception 以外的异常（线程内中断等不可判定
    # 结果）：必须标记 uncertain，close 显式上报，绝不谎报收敛。
    connection = FakePyadsConnection()
    connection.fail["open"] = asyncio.CancelledError()
    session = _session(connection)
    with pytest.raises(asyncio.CancelledError):
        await session.open()
    assert session.is_uncertain
    assert not session.is_open
    with pytest.raises(ADSResourceUncertainError):
        await session.close()


async def test_close_failure_is_uncertain() -> None:
    connection = FakePyadsConnection()
    session = _session(connection)
    await session.open()
    connection.fail["close"] = OSError("transport gone")
    with pytest.raises(OSError):
        await session.close()
    assert session.is_uncertain
    assert not session.is_settled


async def test_close_is_idempotent_and_runs_once() -> None:
    connection = FakePyadsConnection()
    session = _session(connection)
    await session.open()
    results = await asyncio.gather(session.close(), session.close())
    assert results == [None, None]
    assert connection.closed_calls == 1
    assert session.is_settled


async def test_close_rejects_new_commands_even_while_waiting_gate() -> None:
    connection = FakePyadsConnection()
    session = _session(connection)
    await session.open()
    gate = Gate()
    connection.block_before["read_state"] = gate
    occupying = asyncio.create_task(session.execute("read_state"))
    await asyncio.sleep(0.05)
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    with pytest.raises(ADSCommandNotSentError):
        await session.execute("read_state")
    gate.open()
    await occupying
    await closing
    assert session.is_settled
