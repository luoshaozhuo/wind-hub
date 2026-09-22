"""modbus_debug 集成测试 — localhost 真实 Modbus server。

只访问 127.0.0.1 上的 :class:`ModbusMockServer`，绝不访问现场 IP。
覆盖：read all / read one / scale 换算 / int16 写 / int32 写 /
input 点禁止写 / 无 --confirm 禁止写 / raw-read / address-check。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modbus_debug.cli import (
    app,
    raw_read_registers,
    read_device,
    write_device,
)
from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config, DeviceConfig
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.point import Quality

SITE_DIR = Path(__file__).resolve().parents[2] / "configs" / "site_wtg_modbus"
SERVER_PORT = 15202

# 原始寄存器值（mock server 预置）与期望工程值
_RAW_ACTIVE_POWER = 123456  # 0x0001E240 → 工程值 1234.56 kW
_RAW_REACTIVE_POWER = -500  # → 工程值 -5.0 kVar
_RAW_ACTIVE_POWER_1MIN = 100000  # → 工程值 1000.0 kW
_RAW_WIND_SPEED = 850  # → 工程值 8.5 m/s
_RAW_GENERATOR_SPEED = 150055  # 0x00024A27 → 工程值 1500.55 rpm
_RAW_PITCH_ANGLE = 123  # → 工程值 1.23 deg
_RAW_FAULT_CODE = 42
_RAW_TURBINE_STATUS = 7


def _site_input_registers() -> list[int]:
    """按 site_wtg_modbus 点表布局构造输入寄存器块（values[addr] == wire address）。"""
    values = [0] * 1024

    def put_i32(addr: int, raw: int) -> None:
        values[addr] = (raw >> 16) & 0xFFFF  # big_endian：低地址放高字
        values[addr + 1] = raw & 0xFFFF

    values[0] = _RAW_TURBINE_STATUS
    values[19] = _RAW_FAULT_CODE
    put_i32(104, _RAW_GENERATOR_SPEED)
    put_i32(178, _RAW_ACTIVE_POWER)
    put_i32(180, _RAW_REACTIVE_POWER)
    put_i32(204, _RAW_ACTIVE_POWER_1MIN)
    values[357] = _RAW_WIND_SPEED
    put_i32(859, _RAW_PITCH_ANGLE)
    return values


@pytest.fixture
async def site_server() -> AsyncIterator[ModbusMockServer]:
    server = ModbusMockServer(
        port=SERVER_PORT,
        holding=[0] * 2048,
        inputs=_site_input_registers(),
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(scope="module")
def site_config() -> Config:
    return load_config(SITE_DIR)


@pytest.fixture
def local_device(site_config: Config) -> DeviceConfig:
    """wtg-002 配置副本——endpoint 指向 localhost mock server。"""
    dev = next(d for d in site_config.devices.devices if d.device_id == "wtg-002")
    return dev.model_copy(
        update={
            "endpoint": Endpoint(
                host="127.0.0.1",
                port=SERVER_PORT,
                extensions={"unit_id": 1, "timeout": 3.0, "byte_order": "big_endian"},
            )
        }
    )


class TestRead:
    async def test_read_all(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        values, points = await read_device(site_config, local_device, None)
        by_id = {v.point_id: v for v in values}
        # all 组 8 个采集点，不含控制点
        assert set(by_id) == {
            "active_power",
            "active_power_1min",
            "reactive_power",
            "wind_speed",
            "generator_speed",
            "pitch_angle",
            "fault_code",
            "turbine_status",
        }
        assert all(v.quality == Quality.GOOD for v in values)
        assert by_id["active_power"].value == pytest.approx(1234.56)
        assert by_id["reactive_power"].value == pytest.approx(-5.0)
        assert by_id["wind_speed"].value == pytest.approx(8.5)
        assert by_id["fault_code"].value == _RAW_FAULT_CODE
        assert by_id["turbine_status"].value == _RAW_TURBINE_STATUS
        # 点表随结果返回（variable_name/unit 供展示）
        assert {p.point_id for p in points} >= set(by_id)

    async def test_read_one_with_scale(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        values, _ = await read_device(site_config, local_device, "generator_speed")
        assert len(values) == 1
        assert values[0].point_id == "generator_speed"
        # scale=0.01：raw 150055 → 1500.55（经 Device，与正式采集一致）
        assert values[0].value == pytest.approx(1500.55)
        assert values[0].quality == Quality.GOOD


class TestWrite:
    async def test_write_int16(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        result = await write_device(site_config, local_device, "turbine_command", 3)
        assert result.success is True
        # Server 侧确认：单寄存器写到达 2005
        assert await site_server.read_holding(1, 2005) == [3]

    async def test_write_int32(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        result = await write_device(site_config, local_device, "active_power_setpoint", 1000)
        assert result.success is True
        # int32 占 2 寄存器，big_endian：1000 = 0x000003E8 → [0x0000, 0x03E8]
        assert await site_server.read_holding(1, 2001, count=2) == [0x0000, 0x03E8]

    def test_write_input_point_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "write",
                "--config",
                str(SITE_DIR),
                "--device",
                "wtg-002",
                "--point",
                "active_power",  # input register — 只读
                "--value",
                "1",
                "--confirm",
            ],
        )
        assert result.exit_code == 1
        assert "只读" in result.output

    def test_write_without_confirm_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "write",
                "--config",
                str(SITE_DIR),
                "--device",
                "wtg-002",
                "--point",
                "turbine_command",
                "--value",
                "1",
            ],
        )
        assert result.exit_code == 1
        assert "--confirm" in result.output


class TestRawDiagnostics:
    async def test_raw_read(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        registers = await raw_read_registers(local_device, "input", 178, 2)
        assert [r.address for r in registers] == [178, 179]
        # big_endian 高字在前：0x0001, 0xE240
        assert [r.value for r in registers] == [0x0001, 0xE240]

    async def test_raw_read_word_order_candidates(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        registers = await raw_read_registers(local_device, "input", 178, 2)
        import struct

        w0, w1 = registers[0].value, registers[1].value
        assert struct.unpack(">i", struct.pack(">HH", w0, w1))[0] == _RAW_ACTIVE_POWER
        # little_endian（低地址放低字）应得到另一个值——供现场判断 word order
        le = struct.unpack(">i", struct.pack(">HH", w1, w0))[0]
        assert le != _RAW_ACTIVE_POWER

    async def test_address_check_window(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        # address-check 读取 address-1 / address / address+1
        registers = await raw_read_registers(local_device, "input", 357 - 1, 3)
        assert [r.address for r in registers] == [356, 357, 358]
        assert registers[1].value == _RAW_WIND_SPEED
