"""Modbus mock server — 用 pymodbus 起本地从站，供端到端测试采集/下发。

绑定在固定端口（见 :data:`MODBUS_PORT`），与
``tests/fixtures/configs/devices_test.yaml`` 中设备端点一致。预设两组单元
（unit 1 / unit 2）的保持寄存器值：unit 2 供热加载测试新增设备使用。

寄存器布局（wire address）：
    - 100/101  ``rotor.speed`` （float32，big-endian）
    - 102/103  ``gen.power``  （float32）
    - 104      ``temp.int``   （int16）
    - 200/201  ``setpoint.power``（float32，可写，供指令下发测试）
"""

from __future__ import annotations

import struct

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

MODBUS_PORT = 15020

_ADDR_ROTOR_SPEED = 100
_ADDR_GEN_POWER = 102
_ADDR_TEMP_INT = 104
_ADDR_SETPOINT = 200


def _float32_registers(value: float) -> list[int]:
    """Encode a float32 into two big-endian 16-bit register words."""
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def _holding_registers(
    rotor_speed: float,
    gen_power: float,
    temp: int = 25,
    setpoint: float = 0.0,
) -> list[int]:
    """Build a 256-register block with the preset value layout above."""
    values = [0] * 256
    values[_ADDR_ROTOR_SPEED : _ADDR_ROTOR_SPEED + 2] = _float32_registers(rotor_speed)
    values[_ADDR_GEN_POWER : _ADDR_GEN_POWER + 2] = _float32_registers(gen_power)
    values[_ADDR_TEMP_INT] = temp & 0xFFFF
    values[_ADDR_SETPOINT : _ADDR_SETPOINT + 2] = _float32_registers(setpoint)
    return values


class ModbusMockServer:
    """异步生命周期的 Modbus TCP 从站。

    同一实例可 ``start()`` → ``stop()`` → ``start()`` 反复起停（供故障恢复
    测试），每次 ``start()`` 都会重建 server 并绑定同一端口。
    """

    def __init__(self, port: int = MODBUS_PORT) -> None:
        self._port = port
        self._server: ModbusTcpServer | None = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        """启动从站（后台 serve，非阻塞）。"""
        unit1 = ModbusDeviceContext(
            hr=ModbusSequentialDataBlock(1, _holding_registers(1200.5, 800.0)),
        )
        unit2 = ModbusDeviceContext(
            hr=ModbusSequentialDataBlock(1, _holding_registers(900.5, 700.0)),
        )
        # single=False：按 unit id 分派（1 / 2 各自独立数据块）。
        context = ModbusServerContext({1: unit1, 2: unit2}, single=False)
        self._server = ModbusTcpServer(context, address=("127.0.0.1", self._port))
        await self._server.serve_forever(background=True)

    async def stop(self) -> None:
        """关闭从站并释放端口。"""
        if self._server is not None:
            await self._server.shutdown()
            self._server = None
