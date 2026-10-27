"""真实 Modbus 连接失效与重新建链。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolConnectionError
from tests.protocol.conftest import modbus_driver


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
async def test_modbus_server_restart_requires_explicit_reconnect(modbus_server) -> None:
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
