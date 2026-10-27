"""真实 Modbus 从站故障注入：断线与写响应丢失。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolConnectionError
from core.application.protocol_contract import ProtocolWrite, WriteOutcome
from tests.fixtures.servers.modbus_proxy import WriteResponseDropProxy
from tests.protocol.conftest import modbus_driver
from tests.support.process import free_port


@pytest.mark.asyncio
async def test_server_disconnect_invalidates_inflight_session(modbus_server) -> None:
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        assert (await driver.read_one("speed")).value == pytest.approx(1200.5)
        await modbus_server.stop()
        with pytest.raises(ProtocolConnectionError):
            await driver.read_one("speed")
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_restart_requires_explicit_reconnect(modbus_server) -> None:
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        await modbus_server.stop()
        with pytest.raises(ProtocolConnectionError):
            await driver.read_one("speed")
        await driver.close()
        await modbus_server.start()
        await driver.connect()
        assert (await driver.read_one("speed")).value == pytest.approx(1200.5)
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_write_ack_lost_returns_unknown_without_replay(modbus_server) -> None:
    proxy = WriteResponseDropProxy("127.0.0.1", modbus_server.port, free_port())
    await proxy.start()
    driver = modbus_driver(proxy.port, timeout=0.3)
    try:
        await driver.connect()
        proxy.drop_write_responses = True
        result = await driver.write_one(ProtocolWrite("command", 12.5))
        assert result.outcome is WriteOutcome.UNKNOWN
        assert modbus_server.write_count == 1
        assert proxy.dropped_responses == 1
        assert await modbus_server.read_holding(1, 200, 2)
    finally:
        await driver.close()
        await proxy.close()
