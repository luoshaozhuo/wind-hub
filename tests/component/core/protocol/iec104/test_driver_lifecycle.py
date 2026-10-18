"""IEC104 Driver 生命周期、能力边界与会话健康的组件测试。"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from core.application.errors import ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import ProtocolCapability, ProtocolWrite
from core.infrastructure.protocol.iec104.listener import ListenerRegistry
from tests.support.iec104_hat_fake import (
    FakeApciConnection,
    connected_driver,
    floating_data_msg,
    make_driver,
    make_point,
    patch_dial,
)


async def test_capabilities_are_read_write_listen_interrogate_only() -> None:
    driver, _conn = make_driver([make_point("p", 100)])
    assert driver.capabilities() == frozenset(
        {
            ProtocolCapability.READ,
            ProtocolCapability.WRITE,
            ProtocolCapability.LISTEN,
            ProtocolCapability.INTERROGATE,
        }
    )


async def test_batch_and_subscribe_rejected_without_network_io() -> None:
    driver, _conn = make_driver([make_point("p", 100)])

    with pytest.raises(ProtocolCapabilityError, match="batch read"):
        await driver.read_many(("p",))
    with pytest.raises(ProtocolCapabilityError, match="batch write"):
        await driver.write_many((ProtocolWrite(point="p", value=1.0),))
    with pytest.raises(ProtocolCapabilityError, match="subscription"):

        async def _cb(_sample: object) -> None:
            return None

        await driver.subscribe(("p",), _cb)


async def test_listen_requires_established_connection() -> None:
    driver, _conn = make_driver([make_point("p", 100)])

    async def _cb(_sample: object) -> None:
        return None

    with pytest.raises(ProtocolError, match="established connection"):
        await driver.listen(("p",), _cb)


async def test_connect_is_idempotent_and_close_is_idempotent() -> None:
    driver, conn = make_driver([make_point("p", 100)])
    await driver.connect()
    await driver.connect()
    assert driver.is_open()
    await driver.close()
    await driver.close()
    assert driver.is_open() is False
    assert conn._closed is True


async def test_listener_close_timeout_does_not_leak_connection_and_retries() -> None:
    """测试 F：监听回调拒绝结束时连接仍释放；再次 close 收敛遗留任务。"""
    driver, conn = await connected_driver([make_point("p", 100)])
    # 缩短回调收敛时限，让 close 的取消等待确定性地快速超时。
    driver._listeners = ListenerRegistry(callback_timeout=0.05)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def stubborn(_sample: object) -> None:
        entered.set()
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()

    await driver.listen((), stubborn)
    conn.deliver([floating_data_msg(1, 100, 1.0)])
    await entered.wait()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(driver.close(), timeout=2.0)

    # Listener 失败没有跳过连接释放，会话与接收任务已清理。
    assert conn._closed is True
    assert driver.is_open() is False
    assert driver._session is None
    assert len(driver._closing_listeners) == 1
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name() == "iec104-receive"
    ]

    # 回调放行后，再次 close 能确认遗留任务结束；第三次幂等。
    release.set()
    await asyncio.wait_for(driver.close(), timeout=2.0)
    assert driver._closing_listeners == []
    await driver.close()
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and "iec104-listen" in (task.get_name() or "")
    ]


async def test_reconnect_creates_independent_session() -> None:
    driver, conn1 = make_driver([make_point("p", 100)])
    await driver.connect()
    await driver.close()
    conn2 = FakeApciConnection()
    patch_dial(driver, conn2)
    await driver.connect()
    assert driver.is_open()
    await driver.close()
    assert conn1._closed is True and conn2._closed is True


async def test_link_health_reflects_enabled_state() -> None:
    driver, conn = await connected_driver([make_point("p", 100)])
    health = await driver.health()
    assert health.healthy
    assert "link OPEN" in (health.message or "")

    conn.notify_enabled(False)
    health = await driver.health()
    assert not health.healthy
    assert "STARTDT" in (health.message or "")
    await driver.close()


async def test_unconfigured_ca_interrogation_rejected() -> None:
    driver, _conn = await connected_driver(
        [make_point("p", 100)], common_addresses="1,2"
    )
    with pytest.raises(Exception, match="not configured"):
        await driver.interrogate(9)
    await driver.close()


async def test_explicit_ca_interrogation_returns_samples() -> None:
    driver, _conn = make_driver([make_point("p", 100)])
    expected = ("sample-marker",)
    calls: list[tuple[int, int]] = []

    async def interrogate_one(ca: int, *, qoi: int):
        calls.append((ca, qoi))
        return expected

    driver._interrogate_one = interrogate_one  # type: ignore[method-assign]
    result = await driver.interrogate(common_address=1, qoi=20)
    assert result == expected
    assert calls == [(1, 20)]


async def test_cancelled_control_invalidates_session_before_next_command() -> None:
    """取消已经发出的控制，不能复用旧 Session 接收迟到的确认。"""
    from core.application.protocol_contract import ProtocolWrite
    from core.domain import PointAccess

    driver, conn = await connected_driver(
        [make_point("command", 201, type_id="C_SC_NA_1", access=PointAccess.WRITE)]
    )
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="command", value=True)))
    for _ in range(100):
        if conn.sent_messages:
            break
        await asyncio.sleep(0.001)
    assert conn.sent_messages, "control request was not sent"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not driver.is_open()
    assert driver._session is None
    assert len(conn.sent_messages) == 1
    await driver.close()
