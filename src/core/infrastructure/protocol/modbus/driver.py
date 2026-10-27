"""Modbus TCP ProtocolPort；组织测点读写，协议事务交由 Session。"""

from __future__ import annotations

import asyncio
import struct
from itertools import pairwise
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime

from core.application.errors import (
    ConfigError,
    ProtocolCapabilityError,
    ProtocolConnectionError,
    ProtocolError,
)
from core.application.port import ListenHandle, ProtocolSampleCallback, SubscriptionHandle
from core.application.protocol_contract import (
    ConnectionHealth,
    InterrogationRequest,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WriteOutcome,
)
from core.domain import ConnectionEndpoint, PointAccess, PointTable, ProtocolOptions

from .codec import _encode_coil, _encode_registers, decode
from .config import parse_modbus_config
from .mapping import ModbusPoint, group_consecutive_reads, parse_modbus_point
from .session import _ModbusSession

_READ_ONLY = frozenset({"discrete_input", "input"})


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
        self._read_plan_cache: OrderedDict[tuple[str, ...], tuple[tuple[ModbusPoint, ...], ...]] = OrderedDict()
        self._session: _ModbusSession | None = None
        self._lifecycle = asyncio.Lock()
        self.update_point_table(point_table)

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.READ_MANY,
                ProtocolCapability.WRITE,
            }
        )

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
            point.point_id
            for point in point_table.points.values()
            if point.access in (PointAccess.READ, PointAccess.READ_WRITE)
        )
        writable = frozenset(
            point.point_id
            for point in point_table.points.values()
            if point.access in (PointAccess.WRITE, PointAccess.READ_WRITE)
        )
        writable_points = sorted(
            (points[point_id] for point_id in writable),
            key=lambda item: (item.slave_id, item.register_type, item.address),
        )
        for previous, current in pairwise(writable_points):
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
        self._read_plan_cache.clear()

    async def connect(self) -> None:
        async with self._lifecycle:
            if self.is_open():
                return
            previous = self._session
            if previous is not None:
                await previous.close()
                self._session = None
            session = _ModbusSession(self._config)
            self._session = session
            await session.open()

    async def close(self) -> None:
        async with self._lifecycle:
            session = self._session
            if session is None:
                return
            await session.close()
            self._session = None

    def is_open(self) -> bool:
        session = self._session
        return session is not None and session.is_open()

    def _require_session(self) -> _ModbusSession:
        session = self._session
        if session is None or not session.is_open():
            raise ProtocolConnectionError("Modbus TCP connection is not available")
        return session

    def _point(self, point_id: str) -> ModbusPoint:
        point = self._points.get(point_id)
        if point is None:
            raise ConfigError(f"point '{point_id}' not in Modbus table '{self._point_table_id}'")
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
        session = self._require_session()
        raw = await session.read(point.register_type, point.address, point.count, point.slave_id)
        return _sample(point, raw)

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        points = tuple(self._point(point_id) for point_id in point_ids)
        if any(point.point_id not in self._readable for point in points):
            raise ConfigError("Modbus read_many contains a write-only point")
        session = self._require_session()
        key = tuple(point_ids)
        groups = self._read_plan_cache.get(key)
        if groups is None:
            groups = tuple(tuple(group) for group in group_consecutive_reads(points))
            self._read_plan_cache[key] = groups
            if len(self._read_plan_cache) > 32:
                self._read_plan_cache.popitem(last=False)
        else:
            self._read_plan_cache.move_to_end(key)
        samples: dict[ModbusPoint, ProtocolSample] = {}
        for group in groups:
            first = group[0]
            start = min(p.address for p in group)
            count = max(p.address + p.count for p in group) - start
            raw = await session.read(first.register_type, start, count, first.slave_id)
            received_at = datetime.now(UTC)
            for point in group:
                offset = point.address - start
                samples[point] = _sample(point, raw[offset : offset + point.count], received_at)
        return tuple(samples[point] for point in points)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        """编码单个测点并执行一次 Modbus 写事务。"""
        point = self._point(write.point)
        if point.point_id not in self._writable:
            return _result(point.point_id, WriteOutcome.CONFIRMED_FAILURE, "point not writable")
        try:
            if point.register_type in _READ_ONLY:
                raise ValueError(f"{point.register_type} is read-only")
            if point.register_type == "coil":
                values: tuple[int | bool, ...] = (_encode_coil(write.value, point.data_type),)
            else:
                values = tuple(_encode_registers(write.value, point.data_type, point.word_order))
            if len(values) != point.count:
                raise ValueError("write encoder length does not match point span")
        except (TypeError, ValueError, struct.error) as exc:
            return _result(point.point_id, WriteOutcome.CONFIRMED_FAILURE, str(exc))

        outcome, message = await self._require_session().write(
            point.register_type, point.address, values, point.slave_id
        )
        return _result(point.point_id, outcome, message)

    async def write_many(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
        """不支持多逻辑测点批量写入；单测点可使用 FC16。"""
        raise ProtocolCapabilityError("Modbus adapter does not support write_many")

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


def _sample(
    point: ModbusPoint, raw: Sequence[object], received_at: datetime | None = None
) -> ProtocolSample:
    """统一将单个测点原始数据解码为带质量和本地时标的协议样本。"""
    now = received_at or datetime.now(UTC)
    try:
        value = decode(point, raw)
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


def _result(
    point_id: str, outcome: WriteOutcome, message: str | None = None
) -> ProtocolWriteResult:
    return ProtocolWriteResult(point_id=point_id, outcome=outcome, message=message)
