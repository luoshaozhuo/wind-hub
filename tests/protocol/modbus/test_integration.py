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


@pytest.mark.asyncio
async def test_modbus_fc04_reads_input_registers() -> None:
    """FC04 与 FC03 数据区隔离；输入寄存器使用独立的点表。"""
    from core.domain import ConnectionEndpoint, PointAccess
    from core.infrastructure.protocol.modbus import ModbusDriver
    from tests.fixtures.servers.modbus_server import ModbusMockServer
    from tests.protocol.conftest import make_point, make_table
    from tests.support.process import free_port

    inputs = [0] * 256
    inputs[40] = 321
    server = ModbusMockServer(port=free_port(), inputs=inputs)
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", server.port),
        make_table(
            "modbus",
            make_point(
                "input", access=PointAccess.READ, register_type="input",
                address=40, data_type="uint16",
            ),
        ),
        {},
    )
    await server.start()
    try:
        await driver.connect()
        sample = await driver.read_one("input")
        assert sample.value == 321
        assert sample.quality is Quality.GOOD
    finally:
        await driver.close()
        await server.stop()


@pytest.mark.asyncio
async def test_modbus_confirmed_failure_does_not_transmit(modbus_server) -> None:
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        before = modbus_server.write_count
        result = await driver.write_one(ProtocolWrite("command", float("nan")))
        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert modbus_server.write_count == before
    finally:
        await driver.close()
