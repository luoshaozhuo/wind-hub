"""Core-only E2E：YAML 配置目录 → 领域快照 → 驱动 → 真实 Modbus wire 读回。

链路：``YamlConfigAdapter.load()`` 加载 ``configs/example_modbus``，
由快照中的 device/model/point table/protocol options 经
``ProtocolRegistry`` 创建 ``ModbusDriver``，对端为 pymodbus 进程内
真实 TCP Server；断言配置里声明的输入寄存器值经驱动解码后逐点读回。
不经过 collector/commander/server 任何进程层。
"""

from __future__ import annotations

import pytest

from core.application import ProtocolRegistry, Quality
from core.domain import ConnectionEndpoint
from core.infrastructure.config import YamlConfigAdapter
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.support.process import free_port

pytestmark = pytest.mark.real_service

_CONFIG_DIR = "configs/example_modbus"

# active_power: input @178 int32（little_endian 字序：低字在前）
_ACTIVE_POWER_ADDR = 178
_ACTIVE_POWER_VALUE = 150000
# wind_speed: input @357 int16
_WIND_SPEED_ADDR = 357
_WIND_SPEED_VALUE = 121


def _input_registers() -> list[int]:
    regs = [0] * 512
    regs[_ACTIVE_POWER_ADDR] = _ACTIVE_POWER_VALUE & 0xFFFF
    regs[_ACTIVE_POWER_ADDR + 1] = (_ACTIVE_POWER_VALUE >> 16) & 0xFFFF
    regs[_WIND_SPEED_ADDR] = _WIND_SPEED_VALUE
    return regs


async def _start_server(port: int) -> object:
    from pymodbus.datastore import (
        ModbusDeviceContext,
        ModbusSequentialDataBlock,
        ModbusServerContext,
    )
    from pymodbus.server import ModbusTcpServer

    store = ModbusDeviceContext(
        co=ModbusSequentialDataBlock(1, [False] * 16),
        di=ModbusSequentialDataBlock(1, [False] * 16),
        hr=ModbusSequentialDataBlock(1, [0] * 16),
        ir=ModbusSequentialDataBlock(1, _input_registers()),
    )
    server = ModbusTcpServer(
        ModbusServerContext(devices={1: store}, single=False),
        address=("127.0.0.1", port),
    )
    await server.serve_forever(background=True)
    return server


async def test_yaml_config_drives_real_modbus_readback() -> None:
    snapshot = YamlConfigAdapter(_CONFIG_DIR).load()
    device = next(iter(snapshot.site.devices.values()))
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    options = dict(snapshot.protocol_options_by_device.get(device.device_id, {}))
    assert "active_power" in table.points and "wind_speed" in table.points

    port = free_port()
    server = await _start_server(port)
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    driver = registry.create(
        ConnectionEndpoint("127.0.0.1", port),
        table,
        options,
    )
    try:
        await driver.connect()
        assert driver.is_open()

        samples = await driver.read_many(("active_power", "wind_speed", "active_power"))
        assert [s.point_id for s in samples] == [
            "active_power",
            "wind_speed",
            "active_power",
        ]
        assert [s.value for s in samples] == [
            _ACTIVE_POWER_VALUE,
            _WIND_SPEED_VALUE,
            _ACTIVE_POWER_VALUE,
        ]
        assert all(s.quality is Quality.GOOD for s in samples)
        assert all(s.timestamp.tzinfo is not None for s in samples)
        assert all(s.timestamp_source == "local" for s in samples)
    finally:
        await driver.close()
        await server.shutdown()  # type: ignore[attr-defined]
