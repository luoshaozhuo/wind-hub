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
from pymodbus.pdu import ModbusPDU
from pymodbus.server import ModbusTcpServer

MODBUS_PORT = 15020

#: 写类功能码（FC5 写线圈 / FC6 写单寄存器 / FC15 写多线圈 / FC16 写多寄存器）。
_WRITE_FUNCTION_CODES = frozenset({5, 6, 15, 16})

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

    默认寄存器布局见模块 docstring；``holding`` / ``inputs`` 可整体替换
    unit 1 的保持/输入寄存器块（按 wire address 索引的完整寄存器列表），
    供需要自定义点表布局的测试使用（如 example_modbus 配置联调）。

    ``host`` 默认 127.0.0.1；传入其他 loopback 地址（如 127.0.0.2）可构造
    「同机不同 IP」的对端，供 endpoint host 变化类故障测试使用。
    """

    def __init__(
        self,
        port: int = MODBUS_PORT,
        *,
        host: str = "127.0.0.1",
        holding: list[int] | None = None,
        inputs: list[int] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._holding = holding
        self._inputs = inputs
        self._server: ModbusTcpServer | None = None
        self._write_count = 0

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def write_count(self) -> int:
        """从站累计收到的写请求 PDU 数（跨 start/stop 保留，测试自行 reset）。

        通过 pymodbus ``trace_pdu`` 钩子在 **Server 侧** 统计，与被测驱动
        无任何共享状态——用于命令幂等验证（同一 command_id 的并发/重复
        写只允许一次真正到达从站）。
        """
        return self._write_count

    def reset_write_count(self) -> None:
        """清零写请求计数。"""
        self._write_count = 0

    def _trace_pdu(self, sending: bool, pdu: ModbusPDU) -> ModbusPDU:
        """pymodbus trace 钩子：仅统计收到的写请求，不修改报文。"""
        if not sending and pdu.function_code in _WRITE_FUNCTION_CODES:
            self._write_count += 1
        return pdu

    async def start(self) -> None:
        """启动从站（后台 serve，非阻塞）。"""
        unit1_hr = self._holding if self._holding is not None else _holding_registers(1200.5, 800.0)
        unit1 = ModbusDeviceContext(
            hr=ModbusSequentialDataBlock(1, unit1_hr),
            ir=ModbusSequentialDataBlock(1, self._inputs) if self._inputs is not None else None,
        )
        unit2 = ModbusDeviceContext(
            hr=ModbusSequentialDataBlock(1, _holding_registers(900.5, 700.0)),
        )
        # single=False：按 unit id 分派（1 / 2 各自独立数据块）。
        context = ModbusServerContext({1: unit1, 2: unit2}, single=False)
        self._server = ModbusTcpServer(
            context,
            address=(self._host, self._port),
            trace_pdu=self._trace_pdu,
        )
        await self._server.serve_forever(background=True)

    async def stop(self) -> None:
        """关闭从站并释放端口。"""
        if self._server is not None:
            await self._server.shutdown()
            self._server = None

    async def read_holding(self, unit_id: int, address: int, count: int = 1) -> list[int]:
        """用一条独立客户端连接回读从站保持寄存器。

        用于在 **Server 侧** 确认写入确实到达从站（与被测驱动不共享
        任何连接/缓存；pymodbus 3.15 起旧式 context 的内部存储与写入
        路径解耦，直读数据块拿不到写入结果）。
        """
        from pymodbus.client import AsyncModbusTcpClient

        client = AsyncModbusTcpClient(self._host, port=self._port)
        await client.connect()
        try:
            rr = await client.read_holding_registers(address, count=count, device_id=unit_id)
            if rr.isError():
                raise RuntimeError(f"read_holding 回读失败: {rr}")
            return list(rr.registers)
        finally:
            client.close()
