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
                "input",
                access=PointAccess.READ,
                register_type="input",
                address=40,
                data_type="uint16",
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


@pytest.mark.asyncio
async def test_modbus_fc01_fc02_and_fc05_on_real_server() -> None:
    """Coil/DiscreteInput/CoilWrite 均通过实际 Modbus TCP 请求验证。"""
    from core.domain import ConnectionEndpoint, PointAccess
    from core.infrastructure.protocol.modbus import ModbusDriver
    from tests.fixtures.servers.modbus_server import ModbusMockServer
    from tests.protocol.conftest import make_point, make_table
    from tests.support.process import free_port

    coils = [False] * 256
    discretes = [False] * 256
    coils[40] = True
    discretes[41] = True
    server = ModbusMockServer(port=free_port(), coils=coils, discretes=discretes)
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", server.port),
        make_table(
            "modbus",
            make_point("coil", register_type="coil", address=40, data_type="bool"),
            make_point(
                "discrete",
                access=PointAccess.READ,
                register_type="discrete_input",
                address=41,
                data_type="bool",
            ),
        ),
        {},
    )
    await server.start()
    try:
        await driver.connect()
        assert (await driver.read_one("coil")).value is True
        assert (await driver.read_one("discrete")).value is True
        outcome = await driver.write_one(ProtocolWrite("coil", False))
        assert outcome.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert server.write_count == 1
        assert (await driver.read_one("coil")).value is False
    finally:
        await driver.close()
        await server.stop()


@pytest.mark.asyncio
async def test_modbus_slave_identity_isolation(modbus_server) -> None:
    """同一 TCP 服务端两套 Unit ID 的相同寄存器地址必须互不串值。"""
    from core.domain import ConnectionEndpoint, PointAccess
    from core.infrastructure.protocol.modbus import ModbusDriver
    from tests.protocol.conftest import make_point, make_table

    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", modbus_server.port),
        make_table(
            "modbus",
            make_point(
                "unit1", access=PointAccess.READ, register_type="holding",
                address=100, data_type="float32", slave_id=1,
            ),
            make_point(
                "unit2", access=PointAccess.READ, register_type="holding",
                address=100, data_type="float32", slave_id=2,
            ),
        ),
        {"word_order": "big_endian"},
    )
    await driver.connect()
    try:
        result = await driver.read_many(["unit2", "unit1", "unit2"])
        assert [sample.value for sample in result] == pytest.approx(
            [900.5, 1200.5, 900.5]
        )
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_modbus_little_endian_word_order_over_real_tcp() -> None:
    """用真实保持寄存器响应验证 32 位跨寄存器字序转换。"""
    import struct

    from core.domain import ConnectionEndpoint, PointAccess
    from core.infrastructure.protocol.modbus import ModbusDriver
    from tests.fixtures.servers.modbus_server import ModbusMockServer
    from tests.protocol.conftest import make_point, make_table
    from tests.support.process import free_port

    first, second = struct.unpack(">HH", struct.pack(">f", 42.25))
    holding = [0] * 256
    holding[100:102] = [second, first]
    server = ModbusMockServer(port=free_port(), holding=holding)
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", server.port),
        make_table(
            "modbus",
            make_point(
                "value", access=PointAccess.READ, register_type="holding",
                address=100, data_type="float32",
            ),
        ),
        {"word_order": "little_endian"},
    )
    await server.start()
    try:
        await driver.connect()
        assert (await driver.read_one("value")).value == pytest.approx(42.25)
    finally:
        await driver.close()
        await server.stop()
