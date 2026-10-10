"""基于 pymodbus 的共享 Modbus TCP ProtocolPort Adapter。"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Any, NoReturn, overload

from core.application.errors import (
    ConfigError,
    ProtocolCapabilityError,
    ProtocolConnectionError,
    ProtocolError,
)
from core.application.port import ProtocolSampleCallback, SubscriptionHandle
from core.application.protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WritableScalar,
)
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .config import ModbusConfig, parse_modbus_config
from .mapping import ModbusPoint, group_consecutive_reads, parse_modbus_point

if TYPE_CHECKING:
    from pymodbus.client import AsyncModbusTcpClient, ModbusTcpClient
    from pymodbus.exceptions import ModbusException, ModbusIOException
else:
    try:
        from pymodbus.client import AsyncModbusTcpClient, ModbusTcpClient
        from pymodbus.exceptions import ModbusException, ModbusIOException
    except ImportError:  # pymodbus 是可选依赖；缺失时不影响其它协议模块导入。
        AsyncModbusTcpClient = None
        ModbusTcpClient = None
        ModbusException = None
        ModbusIOException = None

_PYMODBUS_MISSING = "Modbus support requires the optional 'pymodbus' dependency"

_BIT_TYPES = frozenset({"coil", "discrete_input"})
_READ_ONLY_TYPES = frozenset({"discrete_input", "input"})
_MULTI_REGISTER_TYPES = frozenset({"int32", "uint32", "float32", "int64", "uint64", "float64"})
_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "int8": (-128, 127),
    "uint8": (0, 255),
    "int16": (-32768, 32767),
    "uint16": (0, 65535),
    "int32": (-2147483648, 2147483647),
    "uint32": (0, 4294967295),
    "int64": (-9223372036854775808, 9223372036854775807),
    "uint64": (0, 18446744073709551615),
}
_DECODE_FAILED = object()


@dataclass(frozen=True, slots=True)
class _ReadGroupPlan:
    points: tuple[ModbusPoint, ...]
    register_type: str
    start: int
    count: int


class ModbusDriver:
    """单个 Endpoint 的 Modbus TCP Driver。

    Driver 只执行单次 connect/read/write，不负责重连退避或采集调度。一个实例
    由单一 asyncio event loop 持有，内部 Lock 防止同一 client 上读写交错。
    """

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        if point_table.protocol.name != "modbus":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'modbus'"
            )

        self._point_table_id = point_table.point_table_id
        self._config: ModbusConfig = parse_modbus_config(
            endpoint,
            protocol_options,
        )
        # 相同选点序列复用预编译读取组；点表热更新时整体失效。
        # 缓存键混用注册点 point_id（str）与动态 ModbusPoint 对象本身——
        # 同名不同地址的动态点与注册点、以及彼此之间的读取计划天然隔离，
        # 不会发生同 point_id 不同地址的结果错配。
        self._read_plan_cache: dict[tuple[str | ModbusPoint, ...], tuple[_ReadGroupPlan, ...]] = {}
        self._points: dict[str, ModbusPoint] = {}
        self.update_point_table(point_table)
        self._lock = asyncio.Lock()
        self._client: Any = None
        # 最近一次真实 Modbus 通信的结果（成功含设备异常响应；失败含错误
        # 描述）；None 表示本连接尚未执行任何通信。
        self._last_exchange: tuple[bool, str] | None = None

    def capabilities(self) -> frozenset[ProtocolCapability]:
        """返回 Modbus Driver 实际支持的协议能力。"""
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
            }
        )

    def update_point_table(self, point_table: PointTable) -> None:
        """热重载点表：重建地址/字序映射并失效读取分组缓存（不断开连接）。"""
        self._points = {
            point.point_id: parse_modbus_point(
                point,
                default_word_order=self._config.word_order,
            )
            for point in point_table.points.values()
        }
        self._read_plan_cache.clear()

    async def connect(self) -> None:
        """建立一次 Modbus TCP 连接；不在 Driver 内部重试。

        以 pymodbus 实际 transport 状态判定复用：现有连接仍打开时直接
        返回（不做无意义的 close/connect）；仅当旧 client 确实失效
        （transport 已断开或已被丢弃）时才替换。
        """
        async with self._lock:
            if AsyncModbusTcpClient is None:
                raise ProtocolError(_PYMODBUS_MISSING)

            if self.is_open():
                return

            self._close_client()
            client = AsyncModbusTcpClient(
                self._config.host,
                port=self._config.port,
                timeout=self._config.timeout,
                retries=0,
                # 禁用 pymodbus transport 层的自动重连（默认 0.1s 起步、
                # 300s 封顶的后台重连任务；falsy 值即不建重连任务）——
                # 重连由 RecoveringProtocol / DeviceRuntime 统一调度，保证连接
                # 生命周期单一权威、重连事件可观测（否则断连在驱动内部
                # 静默愈合）。
                reconnect_delay=0.0,
            )
            try:
                connected = await client.connect()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    client.close()
                raise ProtocolError(
                    f"Modbus connect failed for {self._config.host}:" f"{self._config.port}: {exc}"
                ) from exc

            if not connected:
                with contextlib.suppress(Exception):
                    client.close()
                raise ProtocolError(
                    f"Modbus server rejected connection at "
                    f"{self._config.host}:{self._config.port}"
                )

            self._client = client
            self._last_exchange = None

    async def close(self) -> None:
        """关闭 Modbus client；重复调用安全。"""
        async with self._lock:
            self._close_client()
            self._last_exchange = None

    def is_open(self) -> bool:
        """本地传输连接是否打开——以 pymodbus 实际 transport 状态为准。"""
        client = self._client
        return client is not None and client.connected

    def health(self) -> ConnectionHealth:
        """返回最近一次真实 Modbus 通信的结果，不执行网络探测。

        与 ``is_open()`` 区分：传输打开只说明 TCP 可用，不说明设备能
        应答；尚未执行任何 Modbus 通信的连接不声称通信健康。设备返回的
        有效 Modbus 异常响应证明通信链路可用，计为通信成功（业务失败
        由读取调用本身以 ``ProtocolError`` 表达）。
        """
        if not self.is_open():
            return ConnectionHealth(healthy=False, message="not connected")
        last = self._last_exchange
        if last is None:
            return ConnectionHealth(
                healthy=False,
                message=(
                    f"connected to {self._config.host}:{self._config.port}; "
                    "no Modbus exchange yet"
                ),
            )
        ok, detail = last
        return ConnectionHealth(healthy=ok, message=detail)

    async def read_one(self, point: str | ModbusPoint) -> ProtocolSample:
        """读取一个逻辑点；低频路径，不走批量分组规划或缓存。

        接受注册点 point_id（str）或动态 ModbusPoint（未在点表中登记的
        临时地址）。一个逻辑点可能占多个寄存器（如 float32 占 2 个），但
        仍只发送一次 Modbus 读取请求。解码失败返回 BAD 质量样本；Modbus
        异常响应与通信错误分别抛出 ProtocolError，只有通信错误标记断线。
        """
        mapped = self._resolve_point(point)
        point_id = mapped.point_id
        async with self._lock:
            if not self.is_open():
                raise ProtocolConnectionError("Modbus read requires an active connection")
            try:
                response = await self._read_request(
                    mapped.register_type, mapped.address, mapped.count
                )
            except asyncio.CancelledError:
                self._discard_client("read cancelled")
                raise
            except Exception as exc:
                self._handle_io_failure(exc, "read")

            if response.isError():
                # 设备已应答——通信链路健康，业务失败不作为断线处理。
                self._note_exchange_success("device returned an exception response")
                raise ProtocolError(
                    f"Modbus {mapped.register_type} read at {mapped.address} "
                    f"count={mapped.count} returned an exception response"
                )
            self._note_exchange_success("read succeeded")

            raw: Sequence[object] = (
                response.bits if mapped.register_type in _BIT_TYPES else response.registers
            )
            try:
                value = _decode_point(mapped, raw[: mapped.count])
            except (IndexError, TypeError, ValueError, struct.error):
                return ProtocolSample(
                    point_id=point_id,
                    value=None,
                    timestamp=datetime.now(UTC),
                    quality=Quality.BAD,
                )
            return ProtocolSample(
                point_id=point_id,
                value=_as_point_scalar(value),
                timestamp=datetime.now(UTC),
                quality=Quality.GOOD,
            )

    @overload
    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult: ...

    @overload
    async def write_one(
        self,
        write: str | ModbusPoint,
        value: WritableScalar,
    ) -> ProtocolWriteResult: ...

    async def write_one(
        self,
        write: ProtocolWrite | str | ModbusPoint,
        value: WritableScalar | None = None,
    ) -> ProtocolWriteResult:
        """写入一个逻辑点；低频路径，不做多点合并或写规划。

        两种调用形式：
        - ``write_one(ProtocolWrite(point_id, value))``——注册点写入，
          与通用 ProtocolPort 契约一致；
        - ``write_one(modbus_point, value)`` 或 ``write_one("p1", value)``——
          动态点/注册点的直写便捷形式，校验与第一种完全相同。

        一个逻辑点最多触发一次实际写请求：coil 用 write_coil，单寄存器
        用 write_register，多寄存器逻辑点用一次 write_registers(FC16)。
        只读寄存器类型与取值不合法时在发送前拒绝，不向设备发任何请求。
        """
        if isinstance(write, ProtocolWrite):
            if value is not None:
                raise TypeError("write_one(ProtocolWrite) does not take a separate value")
            mapped = self._mapped_point(write.point_id)
            write_value: WritableScalar = write.value
        else:
            if value is None:
                raise TypeError("write_one(point, value) requires an explicit value")
            mapped = self._resolve_point(write)
            write_value = value
        point_id = mapped.point_id
        if mapped.register_type in _READ_ONLY_TYPES:
            return ProtocolWriteResult(
                point_id=point_id,
                success=False,
                message=f"{mapped.register_type} is read-only",
            )
        async with self._lock:
            if not self.is_open():
                raise ProtocolConnectionError("Modbus write requires an active connection")
            try:
                accepted = await self._write_single(mapped, write_value)
            except (TypeError, ValueError, struct.error) as exc:
                return ProtocolWriteResult(
                    point_id=point_id,
                    success=False,
                    message=str(exc) or type(exc).__name__,
                )
            except ProtocolError:
                raise
            except asyncio.CancelledError:
                self._discard_client("write cancelled")
                raise
            except Exception as exc:
                self._handle_io_failure(exc, "write")
        if not accepted:
            # 设备已应答异常响应——通信链路健康，业务失败不作为断线处理。
            self._note_exchange_success("device returned an exception response")
            return ProtocolWriteResult(
                point_id=point_id,
                success=False,
                message="Modbus exception response",
            )
        self._note_exchange_success("write succeeded")
        return ProtocolWriteResult(point_id=point_id, success=True)

    async def read_many(
        self,
        points: Sequence[str | ModbusPoint],
    ) -> tuple[ProtocolSample, ...]:
        """按连续寄存器分组批量读取，返回按输入顺序排列的样本元组。

        支持注册点 point_id 与动态 ModbusPoint 的混合序列；重复项（同一
        point_id 或地址定义完全相同的 ModbusPoint）按出现位置重复返回，
        顺序与输入严格一致。读取结果以 ModbusPoint（含地址定义）为键，
        同 point_id 不同地址的动态点不会互相覆盖或与注册点混淆。
        """
        mapped_points, values = await self._read_values(points)
        # 全批次共享同一读取时刻——Driver 在获得读取结果时生成 UTC 时间戳。
        received_at = datetime.now(UTC)
        return tuple(
            ProtocolSample(
                point_id=mapped.point_id,
                value=value,
                timestamp=received_at,
                quality=quality,
            )
            for mapped, (value, quality) in zip(mapped_points, values, strict=True)
        )

    async def _read_values(
        self,
        points: Sequence[str | ModbusPoint],
    ) -> tuple[tuple[ModbusPoint, ...], tuple[tuple[PointScalar, Quality], ...]]:
        """执行分组批量读取，返回解析后的点序列与逐点 (原始值, 质量)。"""
        if not points:
            return (), ()

        mapped_points = tuple(self._resolve_point(point) for point in points)
        async with self._lock:
            if not self.is_open():
                raise ProtocolConnectionError("Modbus read requires an active connection")

            plan = self._read_plan(points)
            try:
                values: dict[ModbusPoint, object] = {}
                for group in plan:
                    values.update(await self._read_group(group))
            except ProtocolConnectionError:
                # 链路级失败（传输断开、对端无响应）：不计通信成功，原样上抛。
                raise
            except ProtocolError:
                # 其余 ProtocolError 来自 _read_group 的 Modbus 异常响应：
                # 设备已应答，通信链路健康，业务失败不计为断线。
                self._note_exchange_success("device returned an exception response")
                raise
            except asyncio.CancelledError:
                self._discard_client("read cancelled")
                raise
            except Exception as exc:
                self._handle_io_failure(exc, "read")
            self._note_exchange_success("read succeeded")

            return mapped_points, tuple(
                (None, Quality.BAD)
                if values.get(mapped, _DECODE_FAILED) is _DECODE_FAILED
                else (_as_point_scalar(values[mapped]), Quality.GOOD)
                for mapped in mapped_points
            )

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """Modbus 不支持多逻辑点批量写入；明确拒绝，绝不降级为逐点 write_one。"""
        del writes
        raise NotImplementedError("Modbus write_many is not implemented")

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """Modbus TCP 不支持协议级主动订阅。"""
        del point_ids, callback, interval
        raise ProtocolCapabilityError("modbus does not support subscription")

    async def interrogate(self) -> None:
        """Modbus TCP 不支持 IEC104 式总召能力。"""
        raise ProtocolCapabilityError("modbus does not support interrogation")

    def _mapped_point(self, point_id: str) -> ModbusPoint:
        mapped = self._points.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection " f"'{self._point_table_id}'"
            )
        return mapped

    def _resolve_point(self, point: str | ModbusPoint) -> ModbusPoint:
        """把注册点 point_id 或动态 ModbusPoint 统一解析为 ModbusPoint。

        动态点不进点表映射，只要求调用方给出完整地址定义；其字段合法性
        由 ModbusPoint 构造方（如 ``modbus_point()`` 工厂）保证。
        """
        if isinstance(point, ModbusPoint):
            return point
        return self._mapped_point(point)

    def _read_plan(self, points: Sequence[str | ModbusPoint]) -> tuple[_ReadGroupPlan, ...]:
        """按点位有序序列缓存寄存器分组；命中时跳过映射及分组合并。"""
        key = tuple(points)
        cached = self._read_plan_cache.get(key)
        if cached is not None:
            return cached
        mapped = [self._resolve_point(point) for point in key]
        plan = tuple(
            _ReadGroupPlan(
                points=tuple(group),
                register_type=group[0].register_type,
                start=min(point.address for point in group),
                count=max(point.address + point.count for point in group)
                - min(point.address for point in group),
            )
            for group in group_consecutive_reads(mapped)
        )
        # 典型连续轮询通常只有一个 key；动态请求限制内存增长。
        if len(self._read_plan_cache) >= 32:
            self._read_plan_cache.pop(next(iter(self._read_plan_cache)))
        self._read_plan_cache[key] = plan
        return plan

    async def _read_request(
        self,
        register_type: str,
        start: int,
        count: int,
    ) -> Any:
        """发送一次 Modbus 读取请求并返回原始响应；不检查 isError。"""
        client = self._client
        unit_id = self._config.unit_id
        if register_type == "coil":
            return await client.read_coils(start, count=count, device_id=unit_id)
        if register_type == "discrete_input":
            return await client.read_discrete_inputs(start, count=count, device_id=unit_id)
        if register_type == "holding":
            return await client.read_holding_registers(start, count=count, device_id=unit_id)
        return await client.read_input_registers(start, count=count, device_id=unit_id)

    async def _read_group(
        self,
        group: _ReadGroupPlan,
    ) -> dict[ModbusPoint, object]:
        register_type = group.register_type
        start = group.start
        count = group.count

        response = await self._read_request(register_type, start, count)

        if response.isError():
            raise ProtocolError(
                f"Modbus {register_type} read at {start} count={count} "
                "returned an exception response"
            )

        # 协议响应的序列只读取不修改，避免每轮复制整块寄存器。
        raw: Sequence[object]
        raw = response.bits if register_type in _BIT_TYPES else response.registers

        # 以完整地址定义（ModbusPoint，frozen 可哈希）为键：同 point_id
        # 不同地址的动态点在同组内也不会互相覆盖。
        values: dict[ModbusPoint, object] = {}
        for point in group.points:
            offset = point.address - start
            segment = raw[offset : offset + point.count]
            try:
                values[point] = _decode_point(point, segment)
            except (IndexError, TypeError, ValueError, struct.error):
                values[point] = _DECODE_FAILED
        return values

    async def _write_single(
        self,
        point: ModbusPoint,
        value: object,
    ) -> bool:
        client = self._client
        unit_id = self._config.unit_id

        if point.register_type == "coil":
            coil_value = _encode_coil(value, point.data_type)
            response = await client.write_coil(
                point.address,
                coil_value,
                device_id=unit_id,
            )
        else:
            words = _encode_registers(
                value,
                point.data_type,
                point.word_order,
            )
            if len(words) == 1:
                response = await client.write_register(
                    point.address,
                    words[0],
                    device_id=unit_id,
                )
            else:
                response = await client.write_registers(
                    point.address,
                    words,
                    device_id=unit_id,
                )

        return not response.isError()

    def _note_exchange_success(self, detail: str) -> None:
        """记录一次成功的真实 Modbus 通信（含设备异常响应）。"""
        self._last_exchange = (True, detail)

    def _handle_io_failure(self, exc: Exception, operation: str) -> NoReturn:
        """统一处理读写通信异常：分类、记账，仅在事务不可安全复用时丢弃 client。

        pymodbus 3.15 事务安全性依据（transaction/transaction.py）：

        - ``execute`` 全程持有 per-client Lock，任一时刻只有一个在途请求，
          每次尝试重建 ``response_future``、分配新 TID 并清空接收缓冲；
          响应经 TID/device id 匹配校验，迟到响应不会被错配为有效数据。
        - 请求被取消（含上层 ``wait_for`` 超时取消）或 ``ModbusIOException``
          （对端无响应、TID/device 不匹配）时，请求可能已发出而响应仍在途：
          迟到响应会抢先满足下一次请求的 future 并触发 TID 校验失败，
          使下一次读取承受一次无谓失败。仅这类情形把 client 标记为不可
          复用并丢弃，由下一次恢复重建。
        - 其他通信异常（如连接被拒、对端复位）不携带在途请求；transport
          若已断开由 ``is_open()`` 如实反映，不主动 close。
        """
        cancelled = _unwrap_pymodbus_cancellation(exc)
        if cancelled is not None:
            self._discard_client(f"Modbus {operation} cancelled")
            raise cancelled from exc
        self._last_exchange = (False, f"Modbus {operation} failed: {exc}")
        if ModbusIOException is not None and isinstance(exc, ModbusIOException):
            self._discard_client(f"Modbus {operation} failed: {exc}")
        raise ProtocolConnectionError(f"Modbus {operation} failed: {exc}") from exc

    def _discard_client(self, reason: str) -> None:
        """标记当前 client 不可安全复用并释放；下一次 connect 重建。"""
        self._last_exchange = (False, reason)
        self._close_client()

    def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _unwrap_pymodbus_cancellation(exc: Exception) -> asyncio.CancelledError | None:
    """识别 pymodbus 3.15 包装的请求取消并还原原始 CancelledError。

    还原后由外层 asyncio.wait_for/timeout 区分语义：超时触发的取消转换为
    TimeoutError，上层主动取消则原样传播。只在确证是取消包装时才还原，其他
    通信异常保持原有 ProtocolConnectionError 转换。
    """
    cause = exc.__cause__
    if (
        ModbusException is not None
        and isinstance(exc, ModbusException)
        and isinstance(cause, asyncio.CancelledError)
    ):
        return cause
    return None


def _decode_point(
    point: ModbusPoint,
    segment: Sequence[object],
) -> object:
    if not segment:
        raise ValueError("empty Modbus response segment")

    if point.register_type in _BIT_TYPES:
        raw = segment[0]
        if point.data_type == "bool":
            return bool(raw)
        if isinstance(raw, bool):
            return int(raw)
        if isinstance(raw, int):
            return raw
        raise TypeError("Modbus bit response is not boolean/integer")

    registers = [_strict_register(value) for value in segment]
    return _decode_registers(registers, point.data_type, point.word_order)


def _decode_registers(
    registers: list[int],
    data_type: str,
    word_order: str,
) -> object:
    first = registers[0]
    if data_type == "bool":
        return bool(first & 0x01)
    if data_type == "int8":
        value = first & 0xFF
        return value if value < 0x80 else value - 0x100
    if data_type == "uint8":
        return first & 0xFF
    if data_type == "int16":
        return first if first < 0x8000 else first - 0x10000
    if data_type == "uint16":
        return first

    if data_type not in _MULTI_REGISTER_TYPES:
        raise ValueError(f"unsupported Modbus data type '{data_type}'")

    # 通用 32/64 位编解码交由 PyModbus，避免自行拼接二进制字节流。
    if ModbusTcpClient is None:
        raise ProtocolError(_PYMODBUS_MISSING)
    datatype = getattr(ModbusTcpClient.DATATYPE, data_type.upper(), None)
    if datatype is None:
        raise ValueError(f"unsupported PyModbus data type '{data_type}'")
    return ModbusTcpClient.convert_from_registers(
        registers,
        datatype,
        word_order="little" if word_order == "little_endian" else "big",
    )


def _encode_registers(
    value: object,
    data_type: str,
    word_order: str,
) -> list[int]:
    if data_type == "bool":
        if type(value) is not bool:
            raise TypeError("boolean Modbus point requires bool value")
        return [1 if value else 0]

    integer_range = _INTEGER_RANGES.get(data_type)
    if integer_range is not None:
        number: int | float = _strict_integer_value(value, data_type, integer_range)
        if data_type in {"int8", "uint8", "int16", "uint16"}:
            return [int(number) & 0xFFFF]
    elif data_type in {"float32", "float64"}:
        number = _strict_float_value(value, data_type)
    else:
        raise ValueError(f"unsupported Modbus data type '{data_type}'")

    if ModbusTcpClient is None:
        raise ProtocolError(_PYMODBUS_MISSING)
    datatype = getattr(ModbusTcpClient.DATATYPE, data_type.upper(), None)
    if datatype is None:
        raise ValueError(f"unsupported PyModbus data type '{data_type}'")
    return ModbusTcpClient.convert_to_registers(
        number,
        datatype,
        word_order="little" if word_order == "little_endian" else "big",
    )


def _encode_coil(value: object, data_type: str) -> bool:
    if data_type == "bool":
        if type(value) is not bool:
            raise TypeError("boolean coil requires bool value")
        return value

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("numeric coil requires 0 or 1")
    if value not in (0, 1):
        raise ValueError("numeric coil requires 0 or 1")
    return bool(value)


def _strict_integer_value(
    value: object,
    data_type: str,
    value_range: tuple[int, int],
) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{data_type} requires a numeric integer value")
    integer = int(value)
    if float(value) != float(integer):
        raise ValueError(f"{data_type} requires an integer value")
    lower, upper = value_range
    if not lower <= integer <= upper:
        raise ValueError(f"{data_type} value {integer} outside range {lower}..{upper}")
    return integer


def _strict_float_value(value: object, data_type: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{data_type} requires numeric value")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{data_type} requires a finite value")
    return number


def _strict_register(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Modbus register response must be integer")
    if not 0 <= value <= 0xFFFF:
        raise ValueError("Modbus register outside 0..65535")
    return value


def _as_point_scalar(value: object) -> float | int | bool | str | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported Modbus point value type '{type(value).__name__}'")
