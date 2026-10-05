"""三个协议的本地真实 server 启动（决策 1/11）。

- **Modbus**：pymodbus ``ModbusTcpServer``，预置 ``num_registers`` 个保持
  寄存器（静态值——客户端每次轮询都由驱动盖上新时间戳，延迟语义不受影响）。
- **IEC104**：基于 c104/lib60870-C 装配**压测专用从站**——c104.Server
  承担 STARTDT/TESTFR 握手与总召应答，压测层只做点表预置与周期
  变化上送（SPONTANEOUS transmit 推送任务）。
- **ADS**：``pyads.testserver.AdsTestServer`` + ``AdvancedHandler``，
  预置 ``MAIN.var0..n``（REAL / float32，符号寻址，配 Sum 批量读）。

每个 server 都是 async context manager，端口、变量数可配（决策 11）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import c104
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

logger = logging.getLogger(__name__)

# IEC104 压测点的 IOA 基址（避开 0，贴合现场习惯）。
IEC104_IOA_BASE = 1001


# ---------------------------------------------------------------------------
# Modbus
# ---------------------------------------------------------------------------


def _build_modbus_server(host: str, port: int, num_registers: int) -> ModbusTcpServer:
    """构造预置保持寄存器（unit 1，静态非零值）的 pymodbus server。

    寄存器值取 ``(i * 7) % 0x8000``——确定性伪随机，避免全 0 被误当成
    「没读到数据」。

    注意 pymodbus 的数据块地址是 1-based：``ModbusSequentialDataBlock(1,
    values)`` 使 ``values[i]`` 落在 wire address ``i`` 上（基址 0 会被
    转换成 -1 直接抛错），因此多分配一个槽位。
    """
    values = [(i * 7) % 0x8000 for i in range(num_registers + 1)]
    device = ModbusDeviceContext(hr=ModbusSequentialDataBlock(1, values))
    context = ModbusServerContext({1: device}, single=False)
    return ModbusTcpServer(context, address=(host, port))


@asynccontextmanager
async def start_modbus_server(
    host: str,
    port: int,
    num_registers: int = 10000,
) -> AsyncIterator[ModbusTcpServer]:
    """启动 pymodbus server，预置保持寄存器（unit 1，静态非零值）。"""
    server = _build_modbus_server(host, port, num_registers)
    await server.serve_forever(background=True)
    logger.info("perf Modbus server listening on %s:%d (%d registers)", host, port, num_registers)
    try:
        yield server
    finally:
        await server.shutdown()


class ModbusServerHandle:
    """可重复停/起的 Modbus server 句柄（soak 重连风暴用）。

    pymodbus ``serve_forever(background=True)`` 的 server 在 ``shutdown``
    后不能原地复用，因此每次 :meth:`start` 重建 server 实例；寄存器内容
    由确定性公式重建，停起前后数据语义一致。
    """

    def __init__(self, host: str, port: int, num_registers: int = 10000) -> None:
        self._host = host
        self._port = port
        self._num_registers = num_registers
        self._server: ModbusTcpServer | None = None

    @property
    def running(self) -> bool:
        """server 当前是否在监听。"""
        return self._server is not None

    async def start(self) -> None:
        """启动 server；已在运行时是空操作（幂等）。"""
        if self._server is not None:
            return
        server = _build_modbus_server(self._host, self._port, self._num_registers)
        await server.serve_forever(background=True)
        self._server = server
        logger.info(
            "soak Modbus server listening on %s:%d (%d registers)",
            self._host,
            self._port,
            self._num_registers,
        )

    async def stop(self) -> None:
        """停止 server；未运行时是空操作（幂等）。"""
        if self._server is None:
            return
        server, self._server = self._server, None
        await server.shutdown()


# ---------------------------------------------------------------------------
# IEC104（c104 从站 + 变化上送推送器）
# ---------------------------------------------------------------------------


class _PerfIEC104Slave:
    """压测专用 IEC104 从站：c104.Server + 周期变化上送。

    STARTDT/TESTFR 握手、总召应答与 ASDU 组帧全部由 c104/lib60870-C
    承担；压测层只预置点表（M_ME_NC_1，IOA 自 ``IEC104_IOA_BASE`` 起
    连续编号）并运行一个推送任务：按 ``push_interval_s`` 用正弦波刷新
    全部点的值并以 SPONTANEOUS transmit（决策 5 的「变化上送」）。
    """

    def __init__(
        self,
        host: str,
        port: int,
        num_points: int,
        device_id: str,
        push_interval_s: float = 0.1,
    ) -> None:
        self._host = host
        self._port = port
        self._num_points = num_points
        self._device_id = device_id
        self._push_interval_s = push_interval_s

        self._server: c104.Server | None = None
        self._points: list[c104.Point] = []
        self._push_task: asyncio.Task[None] | None = None
        self._tick = 0

    async def start(self) -> None:
        """构建点表、启动监听与变化上送任务。"""
        server = c104.Server(ip=self._host, port=self._port)
        station = server.add_station(common_address=1)
        if station is None:
            raise RuntimeError("perf IEC104: cannot add station")
        for i in range(self._num_points):
            point = station.add_point(
                io_address=IEC104_IOA_BASE + i, type=c104.Type.M_ME_NC_1
            )
            if point is None:
                raise RuntimeError(f"perf IEC104: cannot add point {i}")
            point.value = 100.0
            self._points.append(point)
        await asyncio.get_running_loop().run_in_executor(None, server.start)
        self._server = server
        self._push_task = asyncio.create_task(self._push_loop())
        logger.info(
            "perf IEC104 slave listening on %s:%d (%d points)",
            self._host,
            self._port,
            self._num_points,
        )

    async def stop(self) -> None:
        """停推送任务 → 停 c104 server（断开全部主站连接；幂等）。"""
        if self._push_task is not None:
            self._push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._push_task
            self._push_task = None
        server, self._server = self._server, None
        self._points.clear()
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(None, server.stop)

    async def _push_loop(self) -> None:
        """周期推送：正弦波刷新全部点的值并逐点 SPONTANEOUS transmit。

        c104 的 Point 更新与 transmit 是线程安全的，可直接在事件循环
        线程调用；监视方向 transmit 无确认语义、不阻塞。
        """
        while True:
            await asyncio.sleep(self._push_interval_s)
            self._tick += 1
            for i, point in enumerate(self._points):
                point.value = 100.0 + 50.0 * math.sin((self._tick + i) / 10.0)
                try:
                    point.transmit(c104.Cot.SPONTANEOUS)
                except Exception:
                    logger.warning(
                        "perf IEC104 spontaneous push failed", exc_info=True
                    )


@asynccontextmanager
async def start_iec104_server(
    host: str,
    port: int,
    num_points: int = 500,
    device_id: str = "perf-iec104",
    push_interval_s: float = 0.1,
) -> AsyncIterator[_PerfIEC104Slave]:
    """启动压测 IEC104 从站（总召应答 + 周期变化上送）。"""
    slave = _PerfIEC104Slave(host, port, num_points, device_id, push_interval_s)
    await slave.start()
    try:
        yield slave
    finally:
        await slave.stop()


# ---------------------------------------------------------------------------
# ADS
# ---------------------------------------------------------------------------


@asynccontextmanager
async def start_ads_server(
    host: str,
    port: int,
    num_vars: int = 600,
) -> AsyncIterator[None]:
    """启动 pyads.testserver，预置 ``MAIN.var0..n``（REAL / float32）。

    变量名与压测点表的 ``symbol`` 地址一一对应，供 ADS Sum 批量读
    （符号寻址）。pyads testserver 跑在自有线程里（``start``/``close``
    都是同步 API），这里只包一层 async CM 统一三协议形态（决策 11）。
    """
    # 延迟导入：pyads testserver 只在 ADS 压测时需要，且 import 链较重。
    from pyads.constants import ADST_REAL32
    from pyads.testserver import AdsTestServer, AdvancedHandler, PLCVariable

    handler = AdvancedHandler()
    for i in range(num_vars):
        handler.add_variable(
            PLCVariable(
                name=f"MAIN.var{i}",
                value=float(i),
                ads_type=ADST_REAL32,
                symbol_type="REAL",
            )
        )
    server = AdsTestServer(handler=handler, ip_address=host, port=port, logging=False)
    server.start()
    logger.info("perf ADS testserver listening on %s:%d (%d vars)", host, port, num_vars)
    try:
        yield
    finally:
        server.close()
        server.join(timeout=5.0)
