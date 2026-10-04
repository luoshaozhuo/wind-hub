"""三个协议的本地真实 server 启动（决策 1/11）。

- **Modbus**：pymodbus ``ModbusTcpServer``，预置 ``num_registers`` 个保持
  寄存器（静态值——客户端每次轮询都由驱动盖上新时间戳，延迟语义不受影响）。
- **IEC104**：复用 step12 的 ``IEC104SlaveSession`` / ``IEC104SlaveHandlers``
  / ``DataSnapshot`` 装配**压测专用从站**。``IEC104SlaveServer`` 本体把
  session 藏在私有集合里、且代理协议本身不含变化上送（spontaneous），
  无法直接满足「总召 + 变化上送」的压测模型（决策 5）——因此这里用同一
  套 step12 构件自行组 session 循环，外加一个周期推送任务；**不修改
  src 下任何代理代码**。
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

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

from wind_hub_collector.adapter.inbound.iec104_slave import (
    DataSnapshot,
    IEC104SlaveHandlers,
)
from wind_hub_collector.adapter.inbound.iec104_slave.handlers import (
    MAX_ASDU_PAYLOAD_BYTES,
    OBJECT_SIZE_BYTES,
)
from wind_hub_collector.adapter.inbound.iec104_slave.session import IEC104SlaveSession
from wind_hub_collector.application.sink_export import SinkReferenceExporter
from wind_hub_core.config.sinks import IEC104SinkAddress, ResolvedSinkPoint, SinkSource
from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    MeasuredValueShort,
    QualityFlag,
    TypeID,
)

logger = logging.getLogger(__name__)

# IEC104 压测点的 IOA 基址（避开 0，贴合现场习惯）。
IEC104_IOA_BASE = 1001
# 变化上送的单 ASDU 对象数。总召应答由 step23 修复后的 handlers 按
# APDU 字节上限自动切分（batch_size 用生产默认 50 即可）；但本从站的
# 推送循环自建 ASDU、绕过 handlers，需自己遵守 253 字节 APDU 上限：
# M_ME_NC_1 每对象 8B → 单帧最多 (253-6)//8 = 30 个对象。
IEC104_BATCH_SIZE = 50
_PUSH_OBJECTS_PER_ASDU = MAX_ASDU_PAYLOAD_BYTES // OBJECT_SIZE_BYTES["M_ME_NC_1"]


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
# IEC104（step12 构件 + 变化上送推送器）
# ---------------------------------------------------------------------------


class _PerfIEC104Slave:
    """压测专用 IEC104 从站：step12 session/handlers + 周期变化上送。

    与生产 :class:`IEC104SlaveServer` 的差异只有两个，且都在压测层实现：
    1. 自持 session 集合（生产 server 把 session 藏在私有集合里，外部
       拿不到句柄，无法做变化上送）；
    2. 一个后台推送任务，按 ``push_interval_s`` 把全部点以 SPONTANEOUS
       原因码广播给每个已 STARTDT 的 session（决策 5 的「变化上送」）。

    总召应答、STARTDT/TESTFR 握手、命令处理完全复用 step12 的
    :class:`IEC104SlaveSession` / :class:`IEC104SlaveHandlers`。
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

        self._snapshot = DataSnapshot()
        self._point_ids = [f"mv.{i:04d}" for i in range(num_points)]
        definitions = [
            ResolvedSinkPoint(
                source=SinkSource(device_id=device_id, point_id=pid),
                ref=f"{device_id}.{pid}",
                source_data_type="float32",
                source_unit="none",
                datatype="float32",
                unit="none",
                address=IEC104SinkAddress(
                    ioa=IEC104_IOA_BASE + i,
                    type_id="M_ME_NC_1",
                ),
            )
            for i, pid in enumerate(self._point_ids)
        ]
        self._exporter = SinkReferenceExporter(definitions)
        self._handlers = IEC104SlaveHandlers(
            snapshot=self._snapshot,
            common_address=1,
            batch_size=IEC104_BATCH_SIZE,
        )

        self._server: asyncio.AbstractServer | None = None
        self._sessions: set[IEC104SlaveSession] = set()
        self._session_tasks: set[asyncio.Task[None]] = set()
        self._push_task: asyncio.Task[None] | None = None
        self._tick = 0

    async def start(self) -> None:
        """监听端口、填充初始快照、启动变化上送任务。"""
        self._refresh_snapshot()
        self._server = await asyncio.start_server(self._on_client, host=self._host, port=self._port)
        self._push_task = asyncio.create_task(self._push_loop())
        logger.info(
            "perf IEC104 slave listening on %s:%d (%d points)",
            self._host,
            self._port,
            self._num_points,
        )

    async def stop(self) -> None:
        """停推送任务 → 断所有 session → 关监听（幂等）。"""
        if self._push_task is not None:
            self._push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._push_task
            self._push_task = None
        for task in list(self._session_tasks):
            task.cancel()
        if self._session_tasks:
            await asyncio.gather(*self._session_tasks, return_exceptions=True)
        self._session_tasks.clear()
        self._sessions.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = IEC104SlaveSession(reader, writer, self._handlers, common_address=1)
        self._sessions.add(session)
        task = asyncio.current_task()
        if task is not None:
            self._session_tasks.add(task)
        try:
            await session.run()
        except Exception:
            logger.warning("perf IEC104 session terminated with error", exc_info=True)
        finally:
            self._sessions.discard(session)
            if task is not None:
                self._session_tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _refresh_snapshot(self) -> None:
        """用正弦波刷新全部点的值（变化上送才有「变化」可送）。"""
        self._tick += 1
        values = [
            PointValue(
                device_id=self._device_id,
                point_id=pid,
                value=100.0 + 50.0 * math.sin((self._tick + i) / 10.0),
            )
            for i, pid in enumerate(self._point_ids)
        ]
        self._snapshot.update(self._exporter.export(values))

    async def _push_loop(self) -> None:
        """周期推送：刷新快照 → 对每个已启动的 session 广播 SPONTANEOUS。"""
        while True:
            await asyncio.sleep(self._push_interval_s)
            self._refresh_snapshot()
            snapshot = self._snapshot.get_all()
            for session in list(self._sessions):
                # session._started 无公开访问器（step12 只暴露 run/send_asdu）；
                # 压测层持有 session 本体，读私有标记避免向未 STARTDT 的
                # 对端乱发 I 帧。
                if not session._started:
                    continue
                for i in range(0, len(snapshot), _PUSH_OBJECTS_PER_ASDU):
                    chunk = snapshot[i : i + _PUSH_OBJECTS_PER_ASDU]
                    asdu = ASDU(
                        type_id=TypeID.M_ME_NC_1,
                        cause=CauseOfTransmission.SPONTANEOUS,
                        common_address=1,
                        objects=[
                            MeasuredValueShort(
                                ioa=ioa, value=float(pv.value), quality=QualityFlag(0)
                            )
                            for ioa, pv in chunk
                        ],
                    )
                    try:
                        await session.send_asdu(asdu)
                    except Exception:
                        logger.warning("perf IEC104 spontaneous push failed", exc_info=True)


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
