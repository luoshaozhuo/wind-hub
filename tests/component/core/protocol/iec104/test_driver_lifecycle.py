"""IEC104 Driver 生命周期、能力边界与会话健康的组件测试。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import ProtocolCapability, ProtocolWrite
from tests.support.iec104_hat_fake import (
    FakeApciConnection,
    connected_driver,
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
