"""IEC104 驱动生命周期：状态机、连接竞争、物理关闭失败 FAULTED、幂等与泄漏。"""

from __future__ import annotations

import asyncio

import pytest

from core.application.errors import ProtocolConnectionError
from core.application.protocol_contract import ProtocolLifecycleState
from core.infrastructure.protocol.iec104.session import _IEC104Session
from tests.support.iec104_fake import (
    FakeConnection,
    make_driver,
    make_point,
)

pytestmark = pytest.mark.asyncio


def set_connect(monkeypatch, conn: FakeConnection) -> None:
    async def _connect(addr, **kwargs):
        del addr, kwargs
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _connect)


def failing_connect(monkeypatch, exc: Exception) -> None:
    async def _connect(addr, **kwargs):
        del addr, kwargs
        raise exc

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _connect)


def state(driver) -> ProtocolLifecycleState:
    return driver.runtime_state().lifecycle


async def test_connect_close_states_and_idempotent(monkeypatch):
    driver = make_driver([make_point("wind", 100)])
    conn = FakeConnection()
    set_connect(monkeypatch, conn)

    assert state(driver) is ProtocolLifecycleState.DISCONNECTED
    await driver.connect()
    assert state(driver) is ProtocolLifecycleState.CONNECTED
    assert driver.is_open()

    await driver.close()
    assert state(driver) is ProtocolLifecycleState.DISCONNECTED
    assert not driver.is_open()

    # 重复 close 幂等，不报错。
    await driver.close()
    assert state(driver) is ProtocolLifecycleState.DISCONNECTED


async def test_connect_already_open_is_noop(monkeypatch):
    driver = make_driver([make_point("wind", 100)])
    set_connect(monkeypatch, FakeConnection())
    await driver.connect()
    await driver.connect()
    assert driver.is_open()
    await driver.close()


async def test_connect_failure_returns_to_disconnected_and_allows_retry(monkeypatch):
    driver = make_driver([make_point("wind", 100)])
    failing_connect(monkeypatch, ConnectionError("dial refused"))

    # P1-b：底层连接错误必须在协议边界包装为 ProtocolConnectionError，不泄漏内置异常。
    with pytest.raises(ProtocolConnectionError) as exc_info:
        await driver.connect()
    assert isinstance(exc_info.value.__cause__, ConnectionError)
    assert state(driver) is ProtocolLifecycleState.DISCONNECTED
    assert not driver.is_open()

    good = FakeConnection()
    set_connect(monkeypatch, good)
    await driver.connect()
    assert driver.is_open()
    await driver.close()


async def test_startdt_never_arrives_times_out_and_allows_reconnect(monkeypatch):
    class NeverEnabled(FakeConnection):
        def register_enabled_cb(self, callback):
            self.enabled_cbs.append(callback)
            return object()  # 不触发 STARTDT 确认

    driver = make_driver([make_point("wind", 100)], options={"t0": 0.2})
    set_connect(monkeypatch, NeverEnabled())
    with pytest.raises(ProtocolConnectionError):
        await driver.connect()
    assert not driver.is_open()

    set_connect(monkeypatch, FakeConnection())
    await driver.connect()
    assert driver.is_open()
    await driver.close()


async def test_physical_close_failure_enters_faulted_and_blocks_reconnect(monkeypatch):
    conn = FakeConnection()
    conn.close_error = RuntimeError("physical close failed")
    driver = make_driver([make_point("wind", 100)])
    set_connect(monkeypatch, conn)
    await driver.connect()

    with pytest.raises(RuntimeError):
        await driver.close()
    assert state(driver) is ProtocolLifecycleState.FAULTED

    # 旧 session 未收敛：禁止建立新连接。
    with pytest.raises(ProtocolConnectionError):
        await driver.connect()


async def test_concurrent_connect_serialized_single_session(monkeypatch):
    driver = make_driver([make_point("wind", 100)])
    set_connect(monkeypatch, FakeConnection())
    await asyncio.gather(driver.connect(), driver.connect())
    assert driver.is_open()
    session = driver._session
    assert isinstance(session, _IEC104Session)
    await driver.close()


async def test_connect_close_race_closes_cleanly(monkeypatch):
    conn = FakeConnection()
    driver = make_driver([make_point("wind", 100)])
    set_connect(monkeypatch, conn)

    connect_task = asyncio.create_task(driver.connect())
    # 与 connect 并发 close；生命周期锁串行化，最终必须断开、无残留连接。
    await asyncio.sleep(0)
    close_task = asyncio.create_task(driver.close())
    await asyncio.gather(connect_task, close_task, return_exceptions=True)
    assert not driver.is_open()
    assert conn.closed


async def test_remote_stopdt_disconnects_open_session(monkeypatch):
    conn = FakeConnection()
    driver = make_driver([make_point("wind", 100)])
    set_connect(monkeypatch, conn)
    await driver.connect()
    conn.notify_enabled(False)
    # 等待接收循环观察 STOPDT。
    await asyncio.sleep(0.05)
    assert not driver.is_open()


async def test_no_receiver_or_listener_task_leftover_after_close(monkeypatch):
    conn = FakeConnection()
    driver = make_driver([make_point("wind", 100)])
    set_connect(monkeypatch, conn)
    await driver.connect()
    receiver = driver._session._receiver
    assert receiver is not None and not receiver.done()

    got = []
    handle = await driver.listen(["wind"], got.append)
    await driver.close()
    # listener 回调任务在 close 时被取消，close() 已等待其收敛。
    assert receiver.cancelled() or receiver.done()
    assert handle.task.done()


async def test_reconnect_after_clean_close_uses_fresh_session(monkeypatch):
    driver = make_driver([make_point("wind", 100)])
    first = FakeConnection()
    set_connect(monkeypatch, first)
    await driver.connect()
    first_session = driver._session
    await driver.close()

    second = FakeConnection()
    set_connect(monkeypatch, second)
    await driver.connect()
    assert driver._session is not first_session
    assert driver.is_open()
    await driver.close()
