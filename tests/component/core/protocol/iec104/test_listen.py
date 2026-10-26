"""本地监听：不触发网络订阅/隐式总召、过滤、溢出与回调异常失效、重连恢复。"""

from __future__ import annotations

import asyncio

import pytest

from tests.support.iec104_fake import (
    FakeConnection,
    floating_data_msg,
    make_driver,
    make_point,
)

pytestmark = pytest.mark.asyncio


def wire(monkeypatch, conn):
    async def _connect(addr, **kwargs):
        del addr, kwargs
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _connect)


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_listen_sends_no_network_message(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100), make_point("b", 101)])
    await driver.connect()
    before = len(conn.sent_messages)
    await driver.listen(["a", "b"], lambda samples: None)
    await settle()
    # listen 只做本地注册：无任何下行报文，无隐式总召。
    assert conn.sent_messages[before:] == []
    await driver.close()


async def test_listen_filters_and_delivers_then_stops_after_close(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100), make_point("b", 101)])
    await driver.connect()
    got_a = []
    handle = await driver.listen(["a"], got_a.append)

    conn.deliver([floating_data_msg(1, 101, 1.0)])  # 不在监听集合
    conn.deliver([floating_data_msg(1, 100, 2.0)])
    await settle()
    assert [b[0].value for b in got_a] == [2.0]

    await handle.close()
    conn.deliver([floating_data_msg(1, 100, 3.0)])
    await settle()
    assert [b[0].value for b in got_a] == [2.0]  # 注销后不再投递
    assert handle not in driver._listeners
    await driver.close()


async def test_no_duplicate_delivery(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)])
    await driver.connect()
    got = []
    await driver.listen(["a"], got.append)
    conn.deliver([floating_data_msg(1, 100, 1.0)])
    await settle()
    assert len(got) == 1
    await driver.close()


async def test_slow_consumer_overflow_invalidates_listener(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)])
    await driver.connect()
    release = asyncio.Event()

    async def blocking(_samples):
        await release.wait()

    handle = await driver.listen(["a"], blocking)
    # 队列容量 256；灌满后溢出，listener 被判失效而非静默丢数据。
    for _ in range(300):
        conn.deliver([floating_data_msg(1, 100, 1.0)])
        await asyncio.sleep(0)
    assert handle.failed is True
    assert handle.task.done() or handle.failed
    release.set()
    await driver.close()


async def test_callback_exception_invalidates_listener(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)])
    await driver.connect()

    async def boom(_samples):
        raise RuntimeError("consumer failure")

    handle = await driver.listen(["a"], boom)
    conn.deliver([floating_data_msg(1, 100, 1.0)])
    for _ in range(30):
        if handle.task.done():
            break
        await asyncio.sleep(0.01)
    assert handle.failed is True
    assert handle.task.done()
    await driver.close()


async def test_listening_resumes_after_watchdog_close_connect(monkeypatch):
    """Watchdog 重连固定为 close()→connect()；恢复后重新 listen 可继续收数。"""
    first = FakeConnection()
    wire(monkeypatch, first)
    driver = make_driver([make_point("a", 100)])
    await driver.connect()
    got = []
    handle = await driver.listen(["a"], got.append)

    # 对端 STOPDT 链路掉线。
    first.notify_enabled(False)
    await settle()
    assert not driver.is_open()

    # Watchdog 重连序列：先收敛旧连接，再建立新连接。
    await driver.close()
    assert handle not in driver._listeners
    second = FakeConnection()
    wire(monkeypatch, second)
    await driver.connect()

    resumed = []
    await driver.listen(["a"], resumed.append)
    second.deliver([floating_data_msg(1, 100, 8.0)])
    await settle()
    assert [b[0].value for b in resumed] == [8.0]
    await driver.close()


async def test_empty_point_ids_listens_to_all(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100), make_point("b", 101)])
    await driver.connect()
    got = []
    await driver.listen(None, got.append)
    conn.deliver([floating_data_msg(1, 101, 4.0)])
    await settle()
    assert [b[0].value for b in got] == [4.0]
    await driver.close()
