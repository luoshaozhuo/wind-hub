"""IEC104 公共协议接口与不支持的原生批量操作。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolCapabilityError
from core.application.protocol_contract import ProtocolCapability, ProtocolWrite
from tests.core.protocol.conftest import iec104_driver


def test_iec104_capability_declarations() -> None:
    driver = iec104_driver(12404)
    assert driver.capabilities() == frozenset(
        {
            ProtocolCapability.READ,
            ProtocolCapability.WRITE,
            ProtocolCapability.INTERROGATE,
            ProtocolCapability.LISTEN,
        }
    )


@pytest.mark.asyncio
async def test_iec104_batch_methods_reject_without_network() -> None:
    driver = iec104_driver(12404)
    with pytest.raises(ProtocolCapabilityError):
        await driver.read_many(["power"])
    with pytest.raises(ProtocolCapabilityError):
        await driver.write_many([ProtocolWrite("switch", True)])


@pytest.mark.asyncio
async def test_iec104_health_with_active_listener_does_not_send_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog health 只检查会话和 Listener，不发 C_RD 或隐式重连。"""
    from tests.support.iec104_fake import connect_driver, make_driver, make_point

    driver = make_driver([make_point("power", 100)])
    conn = await connect_driver(driver, monkeypatch)

    async def callback(samples: tuple) -> None:
        del samples

    listener = await driver.listen(["power"], callback)
    try:
        sent = len(conn.sent_messages)
        for _ in range(3):
            result = await driver.health()
            assert result.healthy
        assert len(conn.sent_messages) == sent
        assert driver.is_open()
    finally:
        await listener.close()
        await driver.close()
