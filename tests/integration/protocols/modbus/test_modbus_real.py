"""Modbus 驱动 × 真实 Modbus TCP server 集成测试。

被测组件是 :class:`ModbusDriver` 本身（含 pymodbus 客户端、重连
monitor、编解码路径）；对端是真实的 pymodbus server fixture——
不 monkeypatch 任何一方。写入经 fixture server 独立客户端回读确认。
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.component.collector.conftest import write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port
from tests.support.wait import wait_until
from wind_hub_core.config.loader import load_config
from wind_hub_core.config.schema import DeviceConfig, PointConfig
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointRef, Quality
from wind_hub_core.protocol.modbus.driver import ModbusDriver


def _decode_float32(registers: list[int]) -> float:
    hi, lo = registers
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


def _load_device(config_dir: Path) -> tuple[DeviceConfig, list[PointConfig]]:
    config = load_config(config_dir)
    device = config.devices.devices[0]
    points = list(config.point_tables.tables["modbus"].points)
    return device, points


@pytest.fixture
async def modbus_driver(tmp_path: Path) -> AsyncIterator[tuple[ModbusDriver, ModbusMockServer]]:
    port = free_port()
    server = ModbusMockServer(port=port)
    config_dir = write_functional_config(tmp_path / "cfg", port)
    device_cfg, points = _load_device(config_dir)
    await server.start()
    driver = ModbusDriver(device_cfg)
    driver.set_points_mapping(points)
    try:
        yield driver, server
    finally:
        await driver.close()
        await server.stop()


def _ref(point_id: str) -> PointRef:
    return PointRef(device_id="modbus-1", point_id=point_id)


class TestConnectAndRead:
    async def test_connect_reports_healthy(self, modbus_driver) -> None:
        driver, _ = modbus_driver
        await driver.connect()
        assert driver.health().healthy is True

    async def test_read_batch_decodes_all_data_types(self, modbus_driver) -> None:
        driver, _ = modbus_driver
        await driver.connect()
        values = await driver.read(
            [_ref("rotor.speed"), _ref("gen.power"), _ref("temp.int")]
        )
        by_id = {v.point_id: v for v in values}
        assert by_id["rotor.speed"].value == pytest.approx(1200.5)
        assert by_id["gen.power"].value == pytest.approx(800.0)
        assert by_id["temp.int"].value == 25
        assert all(v.quality is Quality.GOOD for v in values)

    async def test_read_unknown_point_returns_bad_quality(self, modbus_driver) -> None:
        driver, _ = modbus_driver
        await driver.connect()
        values = await driver.read([_ref("ghost.point")])
        assert values[0].value is None
        assert values[0].quality is Quality.BAD

    async def test_read_unmapped_register_raises_protocol_error(
        self, modbus_driver, tmp_path: Path
    ) -> None:
        # 地址超出 fixture 数据块（256 寄存器）——server 返回异常响应。
        driver, _ = modbus_driver
        config_dir = tmp_path / "cfg"
        _, points = _load_device(config_dir)
        far_point = PointConfig(
            point_id="too.far",
            point_groups=["telemetry"],
            address={"register_type": "holding", "address": 4000},
            data_type="int16",
        )
        driver.set_points_mapping([*points, far_point])
        await driver.connect()
        with pytest.raises(ProtocolError, match="exception response"):
            await driver.read([_ref("too.far")])

    async def test_read_without_connect_raises(self, modbus_driver) -> None:
        driver, _ = modbus_driver
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([_ref("rotor.speed")])


class TestWrite:
    async def test_write_reaches_server_registers(self, modbus_driver) -> None:
        driver, server = modbus_driver
        await driver.connect()
        results = await driver.write(
            [
                Command(
                    command_id="w-1",
                    device_id="modbus-1",
                    point_id="setpoint.power",
                    value=42.5,
                )
            ]
        )
        assert results[0].success, results[0].error
        registers = await server.read_holding(unit_id=1, address=200, count=2)
        assert _decode_float32(registers) == pytest.approx(42.5)


class TestReconnect:
    async def test_driver_recovers_after_server_restart(self, modbus_driver) -> None:
        driver, server = modbus_driver
        await driver.connect()

        await server.stop()
        with pytest.raises(ProtocolError):
            await driver.read([_ref("rotor.speed")])

        await server.start()
        # monitor 后台重连：轮询直到读恢复。
        async def _probe() -> list | None:
            try:
                values = await driver.read([_ref("rotor.speed")])
            except ProtocolError:
                return None
            return values if values[0].quality is Quality.GOOD else None

        values = await wait_until(
            _probe, timeout=15.0, description="modbus driver reconnects"
        )
        assert values[0].value == pytest.approx(1200.5)
        assert driver.health().healthy is True


class TestClose:
    async def test_close_disconnects_and_rejects_read(self, modbus_driver) -> None:
        driver, _ = modbus_driver
        await driver.connect()
        await driver.close()
        assert driver.health().healthy is False
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([_ref("rotor.speed")])

    async def test_close_is_idempotent(self, modbus_driver) -> None:
        driver, _ = modbus_driver
        await driver.connect()
        await driver.close()
        await driver.close()
