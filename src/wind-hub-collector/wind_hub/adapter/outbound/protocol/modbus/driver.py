"""基于 pymodbus 的 Modbus TCP ProtocolPort 实现。

点位按 register_type + address 寻址，相邻地址在读取前合并为单个 Modbus 请求。
Modbus 为请求/响应协议，因此 acquisition_mode 固定为 POLL，subscribe 明确不支持。

pymodbus client 在 connect 时延迟导入和实例化，使未安装 modbus extra 的基础环境
仍可导入 Collector。client 类型只在适配器内部以 Any 隔离，不进入公开接口。

Driver 由单个 asyncio event loop 持有，内部 Lock 串行化 read/write；传输异常
统一包装为 ProtocolError，并触发指数退避后台重连。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Awaitable, Callable
from typing import Any

from wind_hub.adapter.outbound.protocol.modbus.config import ModbusConfig, from_device_config
from wind_hub.adapter.outbound.protocol.modbus.mapping import (
    ModbusPoint,
    group_consecutive_reads,
    parse_point,
)
from wind_hub_core.config.schema import DeviceConfig, PointConfig
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.errors import CommandError, ProtocolError
from wind_hub_core.model.point import PointRef, PointValue, Quality
from wind_hub_core.model.health import HealthStatus
from wind_hub.domain.port.outbound import (
    AcquisitionMode,
    ProtocolPort,
    SubscriptionHandle,
)

logger = logging.getLogger(__name__)

# 指数退避参数；上限与重试预算由 ModbusConfig 提供。
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_MULTIPLIER = 2.0

# bit-addressed 与 word-addressed 点使用不同解码路径；input 虽只读但属于 word 类型。
_BIT_TYPES = frozenset({"coil", "discrete_input"})

# 只读 register_type；coil 与 holding 允许写。
_READ_ONLY_TYPES = frozenset({"discrete_input", "input"})

# 多寄存器数据类型的 struct 格式；单寄存器类型走直接转换路径。
_MULTI_REGISTER_FMT: dict[str, str] = {
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
    "int64": ">q",
    "uint64": ">Q",
    "float64": ">d",
}


def _decode_registers(registers: list[int], data_type: str, word_order: str) -> Any:
    """把连续 16-bit register 解码为点值。

    Any 仅表示点表允许的多种标量结果类型。
    """
    if data_type == "bool":
        return bool(registers[0] & 0x01)
    if data_type == "int8":
        return registers[0] & 0xFF if registers[0] < 0x80 else (registers[0] & 0xFF) - 0x100
    if data_type == "uint8":
        return registers[0] & 0xFF
    if data_type == "int16":
        return registers[0] if registers[0] < 0x8000 else registers[0] - 0x10000
    if data_type == "uint16":
        return registers[0]

    fmt = _MULTI_REGISTER_FMT[data_type]
    words = list(registers)
    if word_order == "little_endian":
        # little_endian word order 下，低地址 register 保存低有效字。
        words = list(reversed(words))
    raw = b"".join(struct.pack(">H", w) for w in words)
    return struct.unpack(fmt, raw)[0]


#: 单点 decode 失败的哨兵值——整组请求成功但某个值解不出时，该点以
#: ``Quality.BAD`` 返回，不影响同组其它点（批量读部分失败语义）。
_DECODE_FAILED: Any = object()


def _encode_registers(value: Any, data_type: str, word_order: str) -> list[int]:
    """把命令值编码为 16-bit register 列表。

    Any 仅对应 Command.value 的动态标量边界；实际编码由 data_type 约束。
    """
    if data_type == "bool":
        return [1 if value else 0]
    if data_type == "int8":
        return [int(value) & 0xFF]
    if data_type == "uint8":
        return [int(value) & 0xFF]
    if data_type == "int16":
        return [int(value) & 0xFFFF]
    if data_type == "uint16":
        return [int(value) & 0xFFFF]

    fmt = _MULTI_REGISTER_FMT[data_type]
    raw = struct.pack(fmt, value)
    words = list(struct.unpack(">" + "H" * (len(raw) // 2), raw))
    if word_order == "little_endian":
        words = list(reversed(words))
    return words


class ModbusDriver:
    """单设备 Modbus TCP 协议驱动。

    Args:
        cfg: 已解析的设备配置。

    Notes:
        实例不保证线程安全；一个 asyncio event loop 独占实例，内部 Lock 保证
        read/write 不会在同一 pymodbus client 上交错执行。
    """

    def __init__(self, cfg: DeviceConfig) -> None:
        self._cfg = cfg
        self._config: ModbusConfig = from_device_config(cfg)
        self._lock = asyncio.Lock()

        self._points: dict[str, ModbusPoint] = {}
        self._connected = False
        self._failed = False
        self._shutdown = False

        self._reconnect_event = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None

        # pymodbus client 在 connect 时延迟导入；Any 仅隔离第三方未类型化对象，
        # 不进入 ProtocolPort 公开接口。
        self._client: Any = None

    # ------------------------------------------------------------------
    # 点表映射
    # ------------------------------------------------------------------

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """注入并解析设备点表。

        Args:
            points: 当前设备使用的 PointConfig 列表。

        Raises:
            ConfigError: 任一点地址或类型配置非法。

        Notes:
            重复 point_id 以后出现者覆盖前者；配置错误在启动/重载阶段快速失败。
        """
        mapping: dict[str, ModbusPoint] = {}
        for point in points:
            mp = parse_point(point, default_word_order=self._config.word_order)
            mapping[point.point_id] = mp
        self._points = mapping

    # ------------------------------------------------------------------
    # ProtocolPort：连接生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """建立 Modbus TCP 连接，并按指数退避执行首次重试。

        Raises:
            NotImplementedError: 配置为 RTU。
            ProtocolError: 首轮连接预算耗尽仍未连接。
        """
        if self._config.mode == "rtu":
            raise NotImplementedError(
                "Modbus RTU is not supported (no pyserial dependency); use mode='tcp'"
            )
        async with self._lock:
            if self._connected:
                return
            self._shutdown = False
            last_exc = await self._connect_with_retry()
            if last_exc is not None:
                raise ProtocolError(
                    f"Modbus: failed to connect to {self._config.host}:{self._config.port} "
                    f"after {self._config.reconnect_max_retries + 1} attempts: {last_exc}"
                ) from last_exc
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def close(self) -> None:
        """关闭 Modbus client 和后台重连任务；重复调用安全。"""
        async with self._lock:
            self._shutdown = True
            self._reconnect_event.set()
            if self._monitor_task is not None and not self._monitor_task.done():
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._monitor_task
            self._monitor_task = None
            self._close_client()
            self._connected = False
            self._failed = False

    async def _connect_with_retry(self) -> Exception | None:
        """执行一次有限预算的指数退避连接。

        Returns:
            成功返回 None；预算耗尽返回最后异常并标记 failed。
        """
        backoff = _RECONNECT_BACKOFF_BASE
        last_exc: Exception | None = None
        budget = self._config.reconnect_max_retries + 1
        for attempt in range(budget):
            try:
                await self._do_connect()
                self._connected = True
                self._failed = False
                logger.info(
                    "Modbus: connected to %s:%d (unit %d)",
                    self._config.host,
                    self._config.port,
                    self._config.unit_id,
                )
                return None
            except Exception as exc:  # connection failure — retry with backoff
                last_exc = exc
                if isinstance(exc, TimeoutError):
                    # 连接/读写超时是对端不可达的日常表现：明确标记为超时，
                    # 保持简洁 warning、不打堆栈（决策 2）。
                    logger.warning(
                        "Modbus: connect attempt %d/%d timed out: %s", attempt + 1, budget, exc
                    )
                else:
                    logger.warning(
                        "Modbus: connect attempt %d/%d failed: %s", attempt + 1, budget, exc
                    )
                if attempt < budget - 1:
                    await asyncio.sleep(backoff)
                    backoff = min(
                        backoff * _RECONNECT_BACKOFF_MULTIPLIER,
                        self._config.reconnect_backoff_max,
                    )
        self._failed = True
        return last_exc

    async def _do_connect(self) -> None:
        """延迟导入 pymodbus，创建并连接 AsyncModbusTcpClient。

        Raises:
            ImportError: 实际使用 Modbus 但环境未安装 modbus extra。
            ProtocolError: TCP client 返回连接失败。
        """
        from pymodbus.client import AsyncModbusTcpClient

        client = AsyncModbusTcpClient(
            self._config.host,
            port=self._config.port,
            timeout=self._config.timeout,
        )
        connected = await client.connect()
        if not connected:
            client.close()
            raise ProtocolError(
                f"Modbus: server rejected connection at " f"{self._config.host}:{self._config.port}"
            )
        self._client = client

    def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    async def _monitor_loop(self) -> None:
        """断线后在后台按重试策略恢复连接；预算耗尽后停止监视。"""
        while not self._shutdown:
            await self._reconnect_event.wait()
            self._reconnect_event.clear()
            if self._shutdown:
                return
            if self._connected:
                continue
            last_exc = await self._connect_with_retry()
            if last_exc is not None:
                # 重试预算耗尽进入 failed；等待显式 connect() 才重新开始。
                logger.error("Modbus: reconnection retries exhausted: %s", last_exc)
                return

    def _signal_disconnect(self) -> None:
        """标记连接断开，并唤醒后台重连循环。"""
        self._connected = False
        self._reconnect_event.set()

    # ------------------------------------------------------------------
    # ProtocolPort：读取
    # ------------------------------------------------------------------

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """批量读取点位，并把相邻地址合并为较少的 Modbus 请求。

        传输异常统一转换为 ProtocolError 并触发重连；单点解码失败返回
        Quality.BAD，不使同组其他点失败。
        """
        async with self._lock:
            if not self._connected:
                raise ProtocolError("Modbus: cannot read — driver is not connected")
            try:
                return await self._read_impl(points)
            except ProtocolError:
                raise
            except Exception as exc:
                # pymodbus 第三方异常不越过 ProtocolPort；统一包装并触发重连。
                self._signal_disconnect()
                raise ProtocolError(f"Modbus read failed: {exc}") from exc

    async def _read_impl(self, points: list[PointRef]) -> list[PointValue]:
        mp_by_id: dict[str, ModbusPoint] = {}
        for ref in points:
            mp = self._points.get(ref.point_id)
            if mp is not None:
                mp_by_id[ref.point_id] = mp

        values: dict[str, Any] = {}
        for group in group_consecutive_reads(list(mp_by_id.values())):
            values.update(await self._read_group(group))

        results: list[PointValue] = []
        for ref in points:
            mp = mp_by_id.get(ref.point_id)
            if mp is None:
                results.append(
                    PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=None,
                        quality=Quality.BAD,
                        source="modbus",
                    )
                )
            else:
                value = values[ref.point_id]
                results.append(
                    PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=None if value is _DECODE_FAILED else value,
                        quality=Quality.BAD if value is _DECODE_FAILED else Quality.GOOD,
                        source="modbus",
                    )
                )
        return results

    async def _read_group(self, group: list[ModbusPoint]) -> dict[str, Any]:
        client = self._client
        register_type = group[0].register_type
        start = min(p.address for p in group)
        end = max(p.address + p.count for p in group)
        count = end - start
        device_id = self._config.unit_id
        label = f"Modbus: {register_type} read at {start} (count {count})"

        if register_type == "coil":
            response = await client.read_coils(start, count=count, device_id=device_id)
            raw: list[Any] = list(response.bits)
        elif register_type == "discrete_input":
            response = await client.read_discrete_inputs(start, count=count, device_id=device_id)
            raw = list(response.bits)
        elif register_type == "holding":
            response = await client.read_holding_registers(start, count=count, device_id=device_id)
            raw = list(response.registers)
        else:  # "input"
            response = await client.read_input_registers(start, count=count, device_id=device_id)
            raw = list(response.registers)

        if response.isError():
            raise ProtocolError(f"{label} returned a Modbus exception response")

        values: dict[str, Any] = {}
        for p in group:
            offset = p.address - start
            segment = raw[offset : offset + p.count]
            try:
                if register_type in _BIT_TYPES:
                    values[p.point_id] = (
                        bool(segment[0]) if p.data_type == "bool" else int(segment[0])
                    )
                else:
                    values[p.point_id] = _decode_registers(segment, p.data_type, p.word_order)
            except Exception:
                # 单点 decode 失败（寄存器数不足/类型不符，多为点表配置问题）
                # 只影响该点——标记 BAD，不让整组失败（部分失败语义）。
                logger.warning(
                    "Modbus: decode failed for point '%s' (type %s) — marked BAD",
                    p.point_id,
                    p.data_type,
                )
                values[p.point_id] = _DECODE_FAILED
        return values

    # ------------------------------------------------------------------
    # ProtocolPort：写入
    # ------------------------------------------------------------------

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """批量写入命令，并按输入顺序返回 CommandResult。

        只读点和未知点作为单命令失败返回；连接/传输失败抛 ProtocolError。
        """
        async with self._lock:
            if not cmds:
                return []
            if not self._connected:
                raise ProtocolError("Modbus: cannot write — driver is not connected")
            try:
                return await self._write_impl(cmds)
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"Modbus write failed: {exc}") from exc

    async def _write_impl(self, cmds: list[Command]) -> list[CommandResult]:
        client = self._client
        results: list[CommandResult] = []
        for cmd in cmds:
            mp = self._points.get(cmd.point_id)
            if mp is None:
                results.append(self._failed_result(cmd, f"unknown point '{cmd.point_id}'"))
                continue
            if mp.register_type in _READ_ONLY_TYPES:
                results.append(
                    self._failed_result(
                        cmd, f"point '{cmd.point_id}' is read-only ({mp.register_type})"
                    )
                )
                continue
            try:
                await self._write_single(client, cmd, mp)
            except CommandError as exc:
                results.append(self._failed_result(cmd, str(exc)))
            else:
                results.append(CommandResult(command_id=cmd.command_id, success=True))
        return results

    async def _write_single(self, client: Any, cmd: Command, mp: ModbusPoint) -> None:
        device_id = self._config.unit_id
        if mp.register_type == "coil":
            response = await client.write_coil(mp.address, bool(cmd.value), device_id=device_id)
        else:  # holding
            words = _encode_registers(cmd.value, mp.data_type, mp.word_order)
            if len(words) == 1:
                response = await client.write_register(mp.address, words[0], device_id=device_id)
            else:
                response = await client.write_registers(mp.address, words, device_id=device_id)

        if response.isError():
            raise CommandError(
                f"write rejected at address {mp.address} ({mp.register_type})",
                cmd.command_id,
            )

    @staticmethod
    def _failed_result(cmd: Command, error: str) -> CommandResult:
        return CommandResult(command_id=cmd.command_id, success=False, error=error)

    # ------------------------------------------------------------------
    # ProtocolPort：订阅与健康状态
    # ------------------------------------------------------------------

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """Modbus 是纯请求/响应协议——只能主动轮询。"""
        return AcquisitionMode.POLL

    async def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """Modbus 为请求/响应协议，不支持订阅推送。

        Raises:
            NotImplementedError: 始终抛出，调用方应使用 polling。
        """
        raise NotImplementedError(
            "Modbus does not support subscription (request/response protocol)"
        )

    def health(self) -> HealthStatus:
        """返回缓存的连接健康状态；不执行实时网络 I/O。"""
        if self._failed:
            return HealthStatus(healthy=False, message="FAILED: reconnection retries exhausted")
        if not self._connected:
            return HealthStatus(healthy=False, message="not connected")
        return HealthStatus(healthy=True)


# ---------------------------------------------------------------------------
# 协议自注册
# ---------------------------------------------------------------------------

# 注册必须发生在 Driver 类定义完成后；若注册机制改为组合根显式注入，可移除 E402 抑制。
from wind_hub.infra.protocol_registry import register_protocol  # noqa: E402


@register_protocol("modbus")
def _create_modbus(cfg: DeviceConfig) -> ProtocolPort:
    return ModbusDriver(cfg)
