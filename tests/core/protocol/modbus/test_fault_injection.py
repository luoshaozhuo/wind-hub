"""真实 Modbus 从站故障注入：断线与写响应丢失。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolConnectionError
from core.application.protocol_contract import ProtocolWrite, WriteOutcome
from tests.core.protocol.conftest import modbus_driver
from tests.fixtures.servers.modbus_proxy import WriteResponseDropProxy
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


@pytest.mark.asyncio
async def test_malformed_modbus_read_response_invalidates_session() -> None:
    """功能码错配不是业务点 BAD 值；必须关闭不可信会话。"""
    from typing import Any

    from core.infrastructure.protocol.modbus.config import ModbusConfig
    from core.infrastructure.protocol.modbus.session import _ModbusSession

    class FakeResponse:
        function_code = 4
        registers = [101]

        def isError(self) -> bool:  # noqa: N802
            return False

    class FakeClient:
        connected = True

        async def read_holding_registers(self, address: int, *, count: int, device_id: int) -> Any:
            del address, count, device_id
            return FakeResponse()

        def close(self) -> None:
            self.connected = False

    session = _ModbusSession(ModbusConfig(host="127.0.0.1"))
    session._client = FakeClient()  # type: ignore[assignment]
    try:
        with pytest.raises(ProtocolConnectionError, match="function code mismatch"):
            await session.read("holding", 100, 1, 1)
        assert not session.is_open()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_modbus_failed_local_encoding_never_sends_to_plc(modbus_server) -> None:
    """无效写值不会产生 FC06/FC16 的任何网络请求。"""
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        before = modbus_server.write_count
        result = await driver.write_one(ProtocolWrite("command", float("inf")))
        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert modbus_server.write_count == before
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_modbus_queued_write_not_submitted_is_confirmed_failure() -> None:
    """Session 已被禁止提交时，写请求没有进入原生事务。"""
    from core.application.protocol_contract import WriteOutcome
    from core.infrastructure.protocol.modbus.config import ModbusConfig
    from core.infrastructure.protocol.modbus.session import _ModbusSession

    session = _ModbusSession(ModbusConfig(host="127.0.0.1"))
    try:
        outcome, _ = await session.write("holding", 200, [1], 1)
        assert outcome is WriteOutcome.CONFIRMED_FAILURE
    finally:
        await session.close()
