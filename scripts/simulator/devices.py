"""三协议模拟设备：真实协议栈 server + 周期数据刷新 + 写命令落点。

- Modbus：pymodbus ModbusTcpServer，unit 1，holding register 布局
  （float32 big-endian 双寄存器 + int16 单寄存器，见 :data:`MODBUS_LAYOUT`）；
- ADS：pyads ``AdsTestServer``（真实 AMS/TCP 协议栈），符号 ``MAIN.<point>``，
  每台设备绑定独立 loopback IP:48898；
- IEC104：c104 Server，M_ME_NC_1 遥测（IOA 1001+）+ C_SE_NC_1 / C_SC_NA_1
  控制点（IOA 2001/2002），命令确认由 lib60870-C 完成。

每个设备可独立 ``start``/``stop``（故障注入 down）与 ``freeze``/``unfreeze``
（冻结数据生成）。写命令经各协议 server 的写路径落到设备，再由
:meth:`SimDevice.sync_controls` 回同步到生成器（控制点写后保持）。
"""

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass, field

import c104
from pymodbus.datastore.context import DataType
from pymodbus.datastore.context import SimDevice as PymodbusSimDevice
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator.simdata import SimData

from .generators import (
    DeviceGenerator,
    PointSnapshot,
    float32_regs_be,
    regs_to_float32,
)

logger = logging.getLogger(__name__)

#: Modbus 寄存器布局：点名 → (起始 wire address, 寄存器数, 是否 float32)。
MODBUS_LAYOUT: dict[str, tuple[int, int, bool]] = {
    "active_power": (0, 2, True),
    "wind_speed": (2, 2, True),
    "rotor_speed": (4, 2, True),
    "generator_temp": (6, 2, True),
    "pitch_angle": (8, 2, True),
    "fault_code": (10, 1, False),
    "turbine_status": (11, 1, False),
    "power_setpoint": (20, 2, True),
    "turbine_command": (22, 1, False),
}
_MODBUS_BLOCK_SIZE = 64

#: IEC104 IOA 布局：遥测 1001+i（按生成器 floats/ints 顺序），控制点固定。
IEC104_CONTROL_IOA = {"power_setpoint": 2001, "turbine_command": 2002}

ADS_TCP_PORT = 48898


@dataclass
class SimDevice:
    """一台模拟设备的通用外壳：生成器 + 故障状态 + 协议 server 句柄。"""

    device_id: str
    protocol: str
    host: str
    port: int
    seed: int
    generator: DeviceGenerator = field(init=False)
    fault: str | None = None
    _frozen: bool = False

    def __post_init__(self) -> None:
        self.generator = DeviceGenerator(self.seed)

    async def start_server(self) -> None:
        raise NotImplementedError

    async def stop_server(self) -> None:
        raise NotImplementedError

    def apply_snapshot(self, snapshot: PointSnapshot) -> None:
        raise NotImplementedError

    def sync_controls(self) -> None:
        """把 server 侧被写入的控制点值回同步到生成器（默认空实现）。"""

    def freeze(self) -> None:
        self._frozen = True

    def unfreeze(self) -> None:
        self._frozen = False

    async def tick(self, dt_s: float = 1.0) -> None:
        """推进一轮数据生成（冻结时跳过）。"""
        if self._frozen or self.fault == "down":
            return
        self.sync_controls()
        self.apply_snapshot(self.generator.next(dt_s))


class ModbusSimDevice(SimDevice):
    """pymodbus 从站（现代 SimData/SimDevice API）。

    pymodbus 3.15 起旧式 context 内部存储与构造参数解耦（deepcopy），
    必须持有 ``SimCore`` 运行时寄存器列表引用才能原地刷新数据、并观察
    到客户端写入的控制点。
    """

    def __init__(self, *args: object, unit_id: int = 1, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._unit_id = unit_id
        self._server: ModbusTcpServer | None = None
        self._regs: list[int] | None = None
        self._block_start = 0

    async def start_server(self) -> None:
        hr = SimData(
            address=0,
            values=[0] * _MODBUS_BLOCK_SIZE,
            datatype=DataType.REGISTERS,
        )
        device = PymodbusSimDevice(id=self._unit_id, simdata=hr)
        self._server = ModbusTcpServer(device, address=(self.host, self.port))
        block = self._server.context.devices[self._unit_id].block["x"]  # type: ignore[union-attr]
        self._block_start, self._regs = block[0], block[2]
        await self._server.serve_forever(background=True)

    async def stop_server(self) -> None:
        server, self._server, self._regs = self._server, None, None
        if server is not None:
            await server.shutdown()

    def apply_snapshot(self, snapshot: PointSnapshot) -> None:
        if self._regs is None:
            return
        values = {**snapshot.floats, **snapshot.ints}
        for name, (addr, _count, is_float) in MODBUS_LAYOUT.items():
            raw = (
                float32_regs_be(float(values[name]))
                if is_float
                else [int(values[name]) & 0xFFFF]
            )
            base = addr - self._block_start
            for i, reg in enumerate(raw):
                self._regs[base + i] = reg

    def sync_controls(self) -> None:
        if self._regs is None:
            return
        setpoint_addr = MODBUS_LAYOUT["power_setpoint"][0] - self._block_start
        command_addr = MODBUS_LAYOUT["turbine_command"][0] - self._block_start
        setpoint = regs_to_float32(self._regs[setpoint_addr : setpoint_addr + 2])
        command = self._regs[command_addr]
        self.generator.set_control("power_setpoint", setpoint)
        self.generator.set_control("turbine_command", command)


class AdsSimDevice(SimDevice):
    """pyads AdsTestServer；符号 MAIN.<point>（REAL / DINT）。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._variables: dict[str, object] = {}
        self._server: object | None = None

    async def start_server(self) -> None:
        # pyads testserver 为同步线程 API；import 链较重，延迟导入。
        from pyads.constants import ADST_INT32, ADST_REAL32  # type: ignore[import-untyped]
        from pyads.testserver import (  # type: ignore[import-untyped]
            AdsTestServer,
            AdvancedHandler,
            PLCVariable,
        )

        handler = AdvancedHandler()
        snapshot = self.generator.next(0.0)
        values = {**snapshot.floats, **snapshot.ints}
        for name, value in values.items():
            is_float = isinstance(value, float)
            var = PLCVariable(
                name=f"MAIN.{name}",
                value=float(value) if is_float else int(value),
                ads_type=ADST_REAL32 if is_float else ADST_INT32,
                symbol_type="REAL" if is_float else "DINT",
            )
            handler.add_variable(var)
            self._variables[name] = var
        server = AdsTestServer(
            handler=handler, ip_address=self.host, port=self.port, logging=False
        )
        server.start()
        self._server = server

    async def stop_server(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()  # type: ignore[attr-defined]
            server.join(timeout=5.0)  # type: ignore[attr-defined]

    def apply_snapshot(self, snapshot: PointSnapshot) -> None:
        for name, value in snapshot.floats.items():
            var = self._variables.get(name)
            if var is not None:
                var.write(struct.pack("<f", float(value)))  # type: ignore[attr-defined]
        for name, value in snapshot.ints.items():
            var = self._variables.get(name)
            if var is not None:
                var.write(struct.pack("<i", int(value)))  # type: ignore[attr-defined]

    def sync_controls(self) -> None:
        setpoint = self._variables.get("power_setpoint")
        command = self._variables.get("turbine_command")
        if setpoint is not None:
            self.generator.set_control(
                "power_setpoint", struct.unpack("<f", setpoint.value)[0]  # type: ignore[attr-defined]
            )
        if command is not None:
            self.generator.set_control(
                "turbine_command", struct.unpack("<i", command.value)[0]  # type: ignore[attr-defined]
            )


class IEC104SimDevice(SimDevice):
    """c104 从站：遥测 M_ME_NC_1（IOA 1001+）+ 控制点 C_SE_NC_1 / C_SC_NA_1。"""

    def __init__(self, *args: object, common_addr: int = 1, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._common_addr = common_addr
        self._server: c104.Server | None = None
        self._telemetry: dict[str, c104.Point] = {}
        self._control: dict[str, c104.Point] = {}

    async def start_server(self) -> None:
        server = c104.Server(ip=self.host, port=self.port)
        station = server.add_station(common_address=self._common_addr)
        if station is None:
            raise RuntimeError(f"{self.device_id}: cannot add station")
        snapshot = self.generator.next(0.0)
        ioa = 1001
        for name, value in snapshot.floats.items():
            if name in IEC104_CONTROL_IOA:
                continue
            point = station.add_point(io_address=ioa, type=c104.Type.M_ME_NC_1)
            if point is None:
                raise RuntimeError(f"{self.device_id}: cannot add point {name}")
            point.value = float(value)
            self._telemetry[name] = point
            ioa += 1
        # 控制点：C_SE_NC_1（float 设定值）+ C_SC_NA_1（单点命令）。
        setpoint = station.add_point(
            io_address=IEC104_CONTROL_IOA["power_setpoint"], type=c104.Type.C_SE_NC_1
        )
        command = station.add_point(
            io_address=IEC104_CONTROL_IOA["turbine_command"], type=c104.Type.C_SC_NA_1
        )
        if setpoint is None or command is None:
            raise RuntimeError(f"{self.device_id}: cannot add control points")
        setpoint.value = 0.0
        command.value = False
        # 命令回执（ACT_CON/ACT_TERM）由 lib60870-C 自动完成；on_receive
        # 钩子把到达的控制值记录到生成器（Server 侧写确认）。
        setpoint.on_receive(callable=self._on_control)
        command.on_receive(callable=self._on_control)
        self._control = {"power_setpoint": setpoint, "turbine_command": command}
        await asyncio.get_running_loop().run_in_executor(None, server.start)
        self._server = server

    def _on_control(
        self,
        point: c104.Point,
        previous_info: c104.Information,
        message: c104.IncomingMessage,
    ) -> c104.ResponseState:
        """控制命令到达（c104 线程）：按 IOA 回同步到生成器。"""
        if point.io_address == IEC104_CONTROL_IOA["power_setpoint"]:
            self.generator.set_control("power_setpoint", float(point.value))
        elif point.io_address == IEC104_CONTROL_IOA["turbine_command"]:
            self.generator.set_control("turbine_command", int(bool(point.value)))
        else:
            return c104.ResponseState.FAILURE
        return c104.ResponseState.SUCCESS

    async def stop_server(self) -> None:
        server, self._server = self._server, None
        self._telemetry.clear()
        self._control.clear()
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(None, server.stop)

    def apply_snapshot(self, snapshot: PointSnapshot) -> None:
        for name, point in self._telemetry.items():
            point.value = float(snapshot.floats[name])
            with contextlib.suppress(Exception):
                point.transmit(c104.Cot.SPONTANEOUS)
