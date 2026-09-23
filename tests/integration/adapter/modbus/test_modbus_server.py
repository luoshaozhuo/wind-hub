"""Integration test — Modbus driver against a real pymodbus TCP server.

Uses pymodbus's in-process ``ModbusTcpServer`` with an in-memory datastore.

Note on addressing (pymodbus 3.15): ``ModbusSequentialDataBlock(addr, values)``
places ``values[i]`` at *wire* address ``addr - 1 + i``.  We therefore build
every block with ``addr=1`` so that ``values[i]`` lands on wire address ``i``,
which keeps the test readable and independent of that off-by-one quirk.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import Iterator

import pytest

from wind_hub.adapter.outbound.protocol.modbus.driver import ModbusDriver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.command import Command
from wind_hub.domain.model.point import PointRef, Quality

# Wire addresses used by the tests.
_FLOAT_READ_ADDR = 100
_FLOAT_WRITE_ADDR = 200
_COIL_ADDR = 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _float32_registers(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


@pytest.fixture
async def modbus_server() -> Iterator[int]:
    from pymodbus.datastore import (
        ModbusDeviceContext,
        ModbusSequentialDataBlock,
        ModbusServerContext,
    )
    from pymodbus.server import ModbusTcpServer

    # Holding registers: one block whose value[i] maps to wire address i.
    hr_values = [0] * 256
    hr_values[_FLOAT_READ_ADDR : _FLOAT_READ_ADDR + 2] = _float32_registers(1500.5)
    hr = ModbusSequentialDataBlock(1, hr_values)

    # Coils: value[i] → wire coil i.
    co = ModbusSequentialDataBlock(1, [False] * 32)

    store = ModbusDeviceContext(co=co, hr=hr)
    context = ModbusServerContext(devices={1: store}, single=False)

    port = _free_port()
    server = ModbusTcpServer(context, address=("127.0.0.1", port))
    await server.serve_forever(background=True)
    try:
        yield port
    finally:
        await server.shutdown()


def _make_device_config(port: int) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-rtu",
        protocol="modbus",
        point_table="t1",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=port,
            extensions={"unit_id": 1, "timeout": 3.0, "word_order": "big_endian"},
        ),
    )


def _make_point_config(
    point_id: str,
    register_type: str,
    address: int,
    data_type: str = "float32",
) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(register_type=register_type, address=address),
        data_type=data_type,
    )


class TestModbusIntegration:
    async def test_read_holding_float(self, modbus_server: int) -> None:
        driver = ModbusDriver(_make_device_config(modbus_server))
        driver.set_points_mapping([_make_point_config("gen.power", "holding", _FLOAT_READ_ADDR)])

        await driver.connect()
        try:
            values = await driver.read([PointRef(device_id="test-rtu", point_id="gen.power")])
        finally:
            await driver.close()

        assert len(values) == 1
        assert values[0].value == pytest.approx(1500.5)
        assert values[0].quality == Quality.GOOD
        assert values[0].source == "modbus"

    async def test_write_holding_float_round_trip(self, modbus_server: int) -> None:
        driver = ModbusDriver(_make_device_config(modbus_server))
        driver.set_points_mapping(
            [_make_point_config("setpoint.val", "holding", _FLOAT_WRITE_ADDR)]
        )

        await driver.connect()
        try:
            results = await driver.write(
                [
                    Command(
                        command_id="c1",
                        device_id="test-rtu",
                        point_id="setpoint.val",
                        value=42.5,
                    )
                ]
            )
            values = await driver.read([PointRef(device_id="test-rtu", point_id="setpoint.val")])
        finally:
            await driver.close()

        assert results[0].success is True
        assert values[0].value == pytest.approx(42.5)

    async def test_read_and_write_coil(self, modbus_server: int) -> None:
        driver = ModbusDriver(_make_device_config(modbus_server))
        driver.set_points_mapping(
            [_make_point_config("switch.on", "coil", _COIL_ADDR, data_type="bool")]
        )

        await driver.connect()
        try:
            before = await driver.read([PointRef(device_id="test-rtu", point_id="switch.on")])
            results = await driver.write(
                [Command(command_id="c2", device_id="test-rtu", point_id="switch.on", value=True)]
            )
            after = await driver.read([PointRef(device_id="test-rtu", point_id="switch.on")])
        finally:
            await driver.close()

        assert before[0].value is False
        assert results[0].success is True
        assert after[0].value is True
