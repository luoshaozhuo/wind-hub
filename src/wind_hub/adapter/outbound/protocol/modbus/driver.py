"""Modbus protocol driver (TCP over pymodbus).

Implements :class:`~wind_hub.domain.port.outbound.ProtocolPort` for the Modbus
TCP protocol.  Points are addressed by ``register_type`` + ``address`` (see
:mod:`wind_hub.adapter.outbound.protocol.modbus.mapping`); reads of consecutive
addresses are merged into a single request.

Modbus is a pure request/response protocol, so :meth:`subscribe` raises
``NotImplementedError``.  Only TCP transport is implemented; ``rtu`` mode raises
``NotImplementedError`` (no ``pyserial`` dependency is pulled in).
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
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue, Quality
from wind_hub.domain.port.outbound import (
    AcquisitionMode,
    HealthStatus,
    ProtocolPort,
    SubscriptionHandle,
)

logger = logging.getLogger(__name__)

# Exponential-backoff reconnect parameters (shared with reconnect_max_retries /
# reconnect_backoff_max from ModbusConfig).
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_MULTIPLIER = 2.0

# Bit-addressed types (one coil/bit per point) vs word-addressed (16-bit
# registers).  ``input`` is a word type even though it is read-only.
_BIT_TYPES = frozenset({"coil", "discrete_input"})

# read-only register types (coil + holding are writable).
_READ_ONLY_TYPES = frozenset({"discrete_input", "input"})

# ``struct`` format per multi-register data type; ``None`` marks single-register
# types handled directly on the register word.
_MULTI_REGISTER_FMT: dict[str, str] = {
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
    "int64": ">q",
    "uint64": ">Q",
    "float64": ">d",
}


def _decode_registers(registers: list[int], data_type: str, word_order: str) -> Any:
    """Decode contiguous 16-bit register words into a Python value."""
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
        # Lowest-address register holds the least-significant word.
        words = list(reversed(words))
    raw = b"".join(struct.pack(">H", w) for w in words)
    return struct.unpack(fmt, raw)[0]


#: 单点 decode 失败的哨兵值——整组请求成功但某个值解不出时，该点以
#: ``Quality.BAD`` 返回，不影响同组其它点（批量读部分失败语义）。
_DECODE_FAILED: Any = object()


def _encode_registers(value: Any, data_type: str, word_order: str) -> list[int]:
    """Encode a Python value into 16-bit register words."""
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
    """Modbus TCP protocol driver.

    Not thread-safe; a single asyncio event loop owns each instance.  An
    internal :class:`asyncio.Lock` serialises ``read``/``write`` calls so they
    never interleave on the underlying client.
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

        # pymodbus client is imported/instantiated lazily so importing the base
        # package does not require the optional ``modbus`` extra.  Kept as
        # ``Any`` so the third-party type never leaks into this module's API.
        self._client: Any = None

    # ------------------------------------------------------------------
    # point mapping
    # ------------------------------------------------------------------

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """Resolve a point table to :class:`ModbusPoint` entries.

        Duplicate ``point_id`` entries: the last one wins.  Points that fail to
        parse raise :class:`ConfigError` immediately (fail fast at startup).
        """
        mapping: dict[str, ModbusPoint] = {}
        for point in points:
            mp = parse_point(point, default_word_order=self._config.word_order)
            mapping[point.point_id] = mp
        self._points = mapping

    # ------------------------------------------------------------------
    # ProtocolPort — connect / close
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the Modbus server, retrying with exponential backoff."""
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
        """Tear down the connection and stop the reconnect monitor."""
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
        """Attempt connection with exponential backoff.

        Returns:
            ``None`` on success, or the last exception once the retry budget is
            exhausted (after which ``self._failed`` is set).
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
        """Instantiate and connect the pymodbus client (lazy third-party import)."""
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
        """Reconnect in the background after a transport failure is signalled."""
        while not self._shutdown:
            await self._reconnect_event.wait()
            self._reconnect_event.clear()
            if self._shutdown:
                return
            if self._connected:
                continue
            last_exc = await self._connect_with_retry()
            if last_exc is not None:
                # FAILED state — stop monitoring until an explicit connect().
                logger.error("Modbus: reconnection retries exhausted: %s", last_exc)
                return

    def _signal_disconnect(self) -> None:
        """Mark the connection as dropped and request a background reconnect."""
        self._connected = False
        self._reconnect_event.set()

    # ------------------------------------------------------------------
    # ProtocolPort — read
    # ------------------------------------------------------------------

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """Batch-read points, merging consecutive addresses into single requests."""
        async with self._lock:
            if not self._connected:
                raise ProtocolError("Modbus: cannot read — driver is not connected")
            try:
                return await self._read_impl(points)
            except ProtocolError:
                raise
            except Exception as exc:
                # Any other exception here is a transport/connection failure
                # (pymodbus raises its own exception types, which must not leak
                # through the port boundary).  Wrap and schedule reconnection.
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
    # ProtocolPort — write
    # ------------------------------------------------------------------

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """Batch-write commands; one :class:`CommandResult` per command."""
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
    # ProtocolPort — subscribe / health
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
        """Modbus is request/response-only — subscription is not supported."""
        raise NotImplementedError(
            "Modbus does not support subscription (request/response protocol)"
        )

    def health(self) -> HealthStatus:
        """Return cached connection health."""
        if self._failed:
            return HealthStatus(healthy=False, message="FAILED: reconnection retries exhausted")
        if not self._connected:
            return HealthStatus(healthy=False, message="not connected")
        return HealthStatus(healthy=True)


# ---------------------------------------------------------------------------
# self-registration
# ---------------------------------------------------------------------------

from wind_hub.infra.protocol_registry import register_protocol  # noqa: E402


@register_protocol("modbus")
def _create_modbus(cfg: DeviceConfig) -> ProtocolPort:
    return ModbusDriver(cfg)
