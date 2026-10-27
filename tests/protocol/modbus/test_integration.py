"""真实 PyModbus Server 与 Adapter 的 FC03/06/16 交互。"""

from __future__ import annotations

import pytest

from core.application.protocol_contract import ProtocolWrite, Quality, WriteOutcome
from tests.protocol.conftest import modbus_driver


@pytest.mark.asyncio
async def test_modbus_read_one_and_native_grouped_read(modbus_server) -> None:
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        single = await driver.read_one("speed")
        assert single.value == pytest.approx(1200.5)
        assert single.quality is Quality.GOOD
        samples = await driver.read_many(["power", "speed", "power"])
        assert [sample.point_id for sample in samples] == ["power", "speed", "power"]
        assert [sample.value for sample in samples] == pytest.approx([800.0, 1200.5, 800.0])
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_modbus_one_logical_point_fc16(modbus_server) -> None:
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        reply = await driver.write_one(ProtocolWrite("command", 25.5))
        assert reply.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert modbus_server.write_count == 1
        sample = await driver.read_one("command")
        assert sample.value == pytest.approx(25.5)
        result = await driver.write_one(ProtocolWrite("temperature", 31))
        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert modbus_server.write_count == 2
    finally:
        await driver.close()
