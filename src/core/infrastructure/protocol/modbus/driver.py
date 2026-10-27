"""Modbus TCP ProtocolPort；业务读写直达 PyModbus 原生事务。"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymodbus.client import AsyncModbusTcpClient

from core.application.errors import (
    ConfigError,
    ProtocolCapabilityError,
    ProtocolConnectionError,
    ProtocolError,
)
from core.application.port import ListenHandle, ProtocolSampleCallback, SubscriptionHandle
from core.application.port.protocol import ProtocolEventCallback, ProtocolEventHandle
from core.application.protocol_contract import (
    ConnectionHealth,
    InterrogationRequest,
    ProtocolCapability,
    ProtocolLifecycleState,
    ProtocolRuntimeState,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WriteOutcome,
)
from core.domain import ConnectionEndpoint, PointAccess, PointTable, ProtocolOptions
from core.infrastructure.protocol.events import ProtocolEventStream

from .codec import _as_point_scalar, _decode_point, _encode_coil, _encode_registers
from .config import parse_modbus_config
from .mapping import ModbusPoint, group_consecutive_reads, parse_modbus_point
from .session import _ModbusSession

_BITS = frozenset({"coil", "discrete_input"})
_READ_ONLY = frozenset({"discrete_input", "input"})
_MAX_WRITE_BITS = 1968
_MAX_WRITE_WORDS = 123


@dataclass(frozen=True, slots=True)
class _WriteItem:
    index: int
    point: ModbusPoint
    values: tuple[int | bool, ...]


@dataclass(frozen=True, slots=True)
class _WriteBlock:
    items: tuple[_WriteItem, ...]
    start: int
    values: tuple[int | bool, ...]

    @property
    def point(self) -> ModbusPoint:
        return self.items[0].point


class ModbusDriver:
    """一个 endpoint 对应一个当前 Session；不持有第三方请求锁。"""

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        self._config = parse_modbus_config(endpoint, protocol_options)
        self._points: dict[str, ModbusPoint] = {}
        self._point_table_id = ""
        self._readable: frozenset[str] = frozenset()
        self._writable: frozenset[str] = frozenset()
        self._health_point_id: str | None = None
        self._session: _ModbusSession | None = None
        self._lifecycle = asyncio.Lock()
        self._events = ProtocolEventStream()
        self.update_point_table(point_table)

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({
            ProtocolCapability.READ,
            ProtocolCapability.READ_MANY,
            ProtocolCapability.WRITE,
            ProtocolCapability.WRITE_MANY,
        })

    def runtime_state(self) -> ProtocolRuntimeState:
        state = self._events.snapshot().lifecycle
        if state is ProtocolLifecycleState.CONNECTED and not self.is_open():
            state = ProtocolLifecycleState.DISCONNECTED
        return self._events.snapshot(state)

    def observe_events(self, callback: ProtocolEventCallback) -> ProtocolEventHandle:
        return self._events.observe(callback)

    def update_point_table(self, point_table: PointTable) -> None:
        if point_table.protocol.name != "modbus":
            raise ConfigError("Modbus driver requires a Modbus point table")
        points = {
            point.point_id: parse_modbus_point(
                point,
                default_word_order=self._config.word_order,
                default_slave_id=self._config.slave_id,
            )
            for point in point_table.points.values()
        }
        configured = self._config.health_point_id
        if configured is not None and configured not in points:
            raise ConfigError(f"unknown Modbus health point: {configured}")
        readable = frozenset(
            point.point_id for point in point_table.points.values()
            if point.access in (PointAccess.READ, PointAccess.READ_WRITE)
        )
        writable = frozenset(
            point.point_id for point in point_table.points.values()
            if point.access in (PointAccess.WRITE, PointAccess.READ_WRITE)
        )
        writable_points = sorted(
            (points[point_id] for point_id in writable),
            key=lambda item: (item.slave_id, item.register_type, item.address),
        )
        for previous, current in zip(writable_points, writable_points[1:]):
            if (
                previous.slave_id == current.slave_id
                and previous.register_type == current.register_type
                and current.address < previous.address + previous.count
            ):
                raise ConfigError(
                    f"overlapping Modbus writable points: "
                    f"{previous.point_id} and {current.point_id}"
                )
        if configured is not None and configured not in readable:
            raise ConfigError(f"Modbus health point '{configured}' is not readable")
        first_readable = next((key for key in points if key in readable), None)
        self._points = points
        self._readable = readable
        self._writable = writable
        self._point_table_id = point_table.point_table_id
        self._health_point_id = configured or first_readable

    async def connect(self) -> None:
        async with self._lifecycle:
            if self.is_open():
                return
            self._events.transition(ProtocolLifecycleState.CONNECTING)
            previous = self._session
            if previous is not None:
                try:
                    await previous.close()
                except BaseException:
                    self._events.transition(ProtocolLifecycleState.FAULTED)
                    raise
                self._session = None
            session = _ModbusSession(self._config)
            self._session = session
            try:
                await session.open()
            except BaseException:
                self._events.transition(ProtocolLifecycleState.DISCONNECTED)
                raise
            self._events.transition(ProtocolLifecycleState.CONNECTED)

    async def close(self) -> None:
        async with self._lifecycle:
            session = self._session
            if session is None:
                self._events.transition(ProtocolLifecycleState.DISCONNECTED)
                return
            self._events.transition(ProtocolLifecycleState.CLOSING)
            try:
                await session.close()
            except BaseException:
                self._events.transition(ProtocolLifecycleState.FAULTED)
                raise
            self._session = None
            self._events.transition(ProtocolLifecycleState.DISCONNECTED)

    def is_open(self) -> bool:
        session = self._session
        return session is not None and session.connected

    def _connected_session(self) -> _ModbusSession:
        session = self._session
        if (
            self._events.snapshot().lifecycle is not ProtocolLifecycleState.CONNECTED
            or session is None
            or not session.connected
        ):
            raise ProtocolConnectionError("Modbus TCP connection is not available")
        return session

    def _point(self, point_id: str) -> ModbusPoint:
        point = self._points.get(point_id)
        if point is None:
            raise ConfigError(
                f"point '{point_id}' not in Modbus table '{self._point_table_id}'"
            )
        return point

    async def health(self) -> ConnectionHealth:
        if not self.is_open():
            return ConnectionHealth(healthy=False, message="Modbus disconnected")
        if self._health_point_id is None:
            return ConnectionHealth(healthy=False, message="no Modbus health point")
        try:
            sample = await self.read_one(self._health_point_id)
        except ProtocolConnectionError as exc:
            return ConnectionHealth(healthy=False, message=str(exc))
        except ProtocolError as exc:
            return ConnectionHealth(healthy=True, message=f"Modbus reachable: {exc}")
        return ConnectionHealth(
            healthy=True,
            message=f"Modbus probe quality={sample.quality.value}",
        )

    async def read_one(self, point_id: str) -> ProtocolSample:
        point = self._point(point_id)
        if point_id not in self._readable:
            raise ConfigError(f"Modbus point '{point_id}' is not readable")
        session = self._connected_session()
        response = await session.execute(
            lambda client: _read(
                client, point.register_type, point.address, point.count, point.slave_id
            )
        )
        raw = _read_payload(response, point.register_type, point.count)
        return _sample(point, raw)

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        points = tuple(self._point(point_id) for point_id in point_ids)
        if any(point.point_id not in self._readable for point in points):
            raise ConfigError("Modbus read_many contains a write-only point")
        session = self._connected_session()
        samples: dict[ModbusPoint, ProtocolSample] = {}
        for group in group_consecutive_reads(points):
            first = group[0]
            start = min(p.address for p in group)
            count = max(p.address + p.count for p in group) - start
            response = await session.execute(
                lambda client, f=first, a=start, n=count: _read(
                    client, f.register_type, a, n, f.slave_id
                )
            )
            raw = _read_payload(response, first.register_type, count)
            received_at = datetime.now(UTC)
            for point in group:
                offset = point.address - start
                samples[point] = _sample(point, raw[offset:offset + point.count], received_at)
        return tuple(samples[point] for point in points)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        point = self._point(write.point)
        if point.point_id not in self._writable:
            return _result(point.point_id, WriteOutcome.CONFIRMED_FAILURE, "point not writable")
        try:
            item = _make_item(0, point, write.value)
        except (TypeError, ValueError, struct.error) as exc:
            return _result(point.point_id, WriteOutcome.CONFIRMED_FAILURE, str(exc))
        session = self._connected_session()
        block = _make_block((item,))
        return await _execute_write(session, block, point.point_id)

    async def write_many(
        self, writes: Sequence[ProtocolWrite]
    ) -> tuple[ProtocolWriteResult, ...]:
        if not writes:
            return ()
        # 完整预检后才允许任何物理写入，禁止同一请求内重叠地址。
        try:
            if any(write.point not in self._writable for write in writes):
                raise ConfigError("Modbus write_many contains a read-only point")
            items = tuple(
                _make_item(i, self._point(w.point), w.value)
                for i, w in enumerate(writes)
            )
            blocks = _plan_writes(items)
        except (ConfigError, TypeError, ValueError, struct.error) as exc:
            return tuple(_result(w.point, WriteOutcome.CONFIRMED_FAILURE, str(exc)) for w in writes)
        session = self._connected_session()
        results: list[ProtocolWriteResult] = [
            _result(w.point, WriteOutcome.CONFIRMED_FAILURE, "not submitted")
            for w in writes
        ]
        deadline = asyncio.get_running_loop().time() + self._config.timeout
        for block in blocks:
            if asyncio.get_running_loop().time() >= deadline:
                break
            response_result = await _execute_write(session, block, block.point.point_id)
            for item in block.items:
                results[item.index] = _result(
                    item.point.point_id, response_result.outcome, response_result.message
                )
            if response_result.outcome is not WriteOutcome.CONFIRMED_SUCCESS:
                break
        return tuple(results)

    def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        raise ProtocolCapabilityError("Modbus TCP has no remote subscription")

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        raise ProtocolCapabilityError("Modbus TCP has no unsolicited listen")

    async def interrogate(self, request: InterrogationRequest) -> tuple[ProtocolSample, ...]:
        raise ProtocolCapabilityError("Modbus TCP has no interrogation")


async def _read(
    client: AsyncModbusTcpClient,
    register_type: str,
    address: int,
    count: int,
    slave_id: int,
) -> Any:
    if register_type == "coil":
        return await client.read_coils(address, count=count, device_id=slave_id)
    if register_type == "discrete_input":
        return await client.read_discrete_inputs(address, count=count, device_id=slave_id)
    if register_type == "holding":
        return await client.read_holding_registers(address, count=count, device_id=slave_id)
    return await client.read_input_registers(address, count=count, device_id=slave_id)


def _read_payload(response: Any, register_type: str, count: int) -> Sequence[object]:
    if response.isError():
        raise ProtocolError(f"Modbus {register_type} read returned exception response")
    raw: Sequence[object] = response.bits if register_type in _BITS else response.registers
    if len(raw) < count:
        raise ProtocolError(f"short Modbus response: expected {count}, got {len(raw)}")
    return raw


def _sample(
    point: ModbusPoint, raw: Sequence[object], received_at: datetime | None = None
) -> ProtocolSample:
    now = received_at or datetime.now(UTC)
    try:
        value = _as_point_scalar(_decode_point(point, raw))
        quality = Quality.GOOD
    except (IndexError, TypeError, ValueError, struct.error):
        value = None
        quality = Quality.BAD
    return ProtocolSample(
        point_id=point.point_id,
        value=value,
        quality=quality,
        timestamp=now,
        receive_time=now,
    )


def _make_item(index: int, point: ModbusPoint, value: object) -> _WriteItem:
    if point.register_type in _READ_ONLY:
        raise ValueError(f"{point.register_type} is read-only")
    if point.register_type == "coil":
        encoded: tuple[int | bool, ...] = (_encode_coil(value, point.data_type),)
    else:
        encoded = tuple(_encode_registers(value, point.data_type, point.word_order))
    if len(encoded) != point.count:
        raise ValueError("write encoder length does not match point span")
    return _WriteItem(index, point, encoded)


def _make_block(items: tuple[_WriteItem, ...]) -> _WriteBlock:
    return _WriteBlock(
        items=items,
        start=items[0].point.address,
        values=tuple(word for item in items for word in item.values),
    )


def _plan_writes(items: tuple[_WriteItem, ...]) -> tuple[_WriteBlock, ...]:
    ordered = sorted(
        items, key=lambda item: (item.point.slave_id, item.point.register_type, item.point.address)
    )
    result: list[_WriteBlock] = []
    current: list[_WriteItem] = []
    end = 0
    for item in ordered:
        limit = _MAX_WRITE_BITS if item.point.register_type == "coil" else _MAX_WRITE_WORDS
        if len(item.values) > limit:
            raise ValueError("single Modbus point exceeds FC15/FC16 limit")
        if current:
            prev = current[0].point
            same_area = (
                prev.slave_id == item.point.slave_id
                and prev.register_type == item.point.register_type
            )
            if same_area and item.point.address < end:
                raise ValueError(f"overlapping Modbus write at {item.point.address}")
            if (
                not same_area
                or item.point.address != end
                or item.point.address + item.point.count - current[0].point.address > limit
            ):
                result.append(_make_block(tuple(current)))
                current = []
        current.append(item)
        end = item.point.address + item.point.count
    if current:
        result.append(_make_block(tuple(current)))
    return tuple(result)


async def _send_write(client: AsyncModbusTcpClient, block: _WriteBlock) -> Any:
    point = block.point
    if point.register_type == "coil":
        if len(block.values) == 1:
            return await client.write_coil(
                block.start, bool(block.values[0]), device_id=point.slave_id
            )
        return await client.write_coils(
            block.start, [bool(value) for value in block.values], device_id=point.slave_id
        )
    if len(block.values) == 1:
        return await client.write_register(
            block.start, int(block.values[0]), device_id=point.slave_id
        )
    return await client.write_registers(
        block.start, [int(value) for value in block.values], device_id=point.slave_id
    )


async def _execute_write(
    session: _ModbusSession, block: _WriteBlock, point_id: str
) -> ProtocolWriteResult:
    try:
        response = await session.execute(lambda client: _send_write(client, block))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _result(point_id, WriteOutcome.UNKNOWN, str(exc))
    if response.isError():
        # 异常码 4 表示服务器内部故障，无法确认写入是否发生。
        code = getattr(response, "exception_code", None)
        outcome = (
            WriteOutcome.CONFIRMED_FAILURE
            if code in (1, 2, 3)
            else WriteOutcome.UNKNOWN
        )
        return _result(point_id, outcome, f"Modbus exception code: {code}")
    expected_function = (
        5 if block.point.register_type == "coil" and len(block.values) == 1
        else 15 if block.point.register_type == "coil"
        else 6 if len(block.values) == 1
        else 16
    )
    if response.function_code != expected_function or response.address != block.start:
        session.invalidate()
        return _result(point_id, WriteOutcome.UNKNOWN, "Modbus response mismatch")
    if expected_function in (15, 16) and response.count != len(block.values):
        session.invalidate()
        return _result(point_id, WriteOutcome.UNKNOWN, "Modbus write count mismatch")
    if expected_function == 5 and response.bits != [bool(block.values[0])]:
        session.invalidate()
        return _result(point_id, WriteOutcome.UNKNOWN, "Modbus coil echo mismatch")
    if expected_function == 6 and response.registers != [int(block.values[0])]:
        session.invalidate()
        return _result(point_id, WriteOutcome.UNKNOWN, "Modbus register echo mismatch")
    return _result(point_id, WriteOutcome.CONFIRMED_SUCCESS)


def _result(
    point_id: str, outcome: WriteOutcome, message: str | None = None
) -> ProtocolWriteResult:
    return ProtocolWriteResult(point_id=point_id, outcome=outcome, message=message)
