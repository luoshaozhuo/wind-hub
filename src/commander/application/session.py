"""Commander 设备通信会话（Application Runtime 组件）。

DeviceSession 把共享领域对象（Device / PointTable / BusinessPoint）与
``core.application.ProtocolPort`` 组合成单台设备的即时操作边界：

- 读：``ProtocolSample`` 原始值按点表 scale/offset 换算为工程值；
- 写：先检查 ``PointAccess``，再把工程值逆变换为协议原始值后下发；
- 不感知周期调度、采集状态或 Sink——那些属于 Collector。

会话不主动建立连接；连接生命周期由 CommanderRuntime 管理。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from core.application import (
    ConnectionHealth,
    PointScalar,
    ProtocolPort,
    ProtocolSample,
    ProtocolWrite,
    Quality,
    TimestampSource,
    WritableScalar,
    validate_read_many_results,
)
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    Device,
    DeviceId,
    Point,
    PointAccess,
    PointTable,
    engineering_value,
    raw_write_value,
)

from .config import PointMeta
from .errors import CommandError


@dataclass(frozen=True, slots=True)
class PointReading:
    """一次即时读取的工程值结果。

    Attributes:
        device_id: 产生该值的设备标识。
        point_id: 设备点表内 point_id。
        value: 已应用 scale/offset 的工程值；协议失败点为 None。
        quality: 统一数据质量。
        timestamp: 采样时间（UTC，必填）——协议原生时间戳或本地接收时刻。
        timestamp_source: 时间戳来源（``device`` / ``local``）。
        source: 产生该值的协议名。
    """

    device_id: str
    point_id: str
    value: PointScalar
    quality: Quality
    timestamp: datetime
    timestamp_source: TimestampSource
    source: str


class DeviceSession:
    """单台设备的即时通信会话。

    Args:
        device: 共享领域设备聚合。
        point_table: 设备型号绑定的 resolved 点表。
        business_points: 快照中的业务点索引（写值类型参考）。
        point_meta: 本点表的进程级点位元数据。
        protocol: 组合根经 ProtocolRegistry 创建的协议 Driver。
    """

    def __init__(
        self,
        device: Device,
        point_table: PointTable,
        business_points: Mapping[BusinessPointId, BusinessPoint],
        point_meta: Mapping[str, PointMeta],
        protocol: ProtocolPort,
    ) -> None:
        self._device = device
        self._point_table = point_table
        self._business_points = business_points
        self._point_meta = point_meta
        self._protocol = protocol

    @property
    def device_id(self) -> DeviceId:
        """返回设备稳定标识。"""
        return self._device.device_id

    @property
    def device(self) -> Device:
        """返回当前设备领域快照。"""
        return self._device

    @property
    def point_table(self) -> PointTable:
        """返回当前设备绑定点表。"""
        return self._point_table

    @property
    def protocol_name(self) -> str:
        """返回设备点表协议名。"""
        return self._point_table.protocol.name

    @property
    def protocol(self) -> ProtocolPort:
        """返回当前协议运行实例（诊断探测经此执行协议级读取）。"""
        return self._protocol

    def point(self, point_id: str) -> Point:
        """按 point_id 返回点定义；未知点抛 CommandError。"""
        point = self._point_table.points.get(point_id)
        if point is None:
            raise CommandError(f"unknown point '{self._device.device_id}/{point_id}'")
        return point

    def point_meta(self, point_id: str) -> PointMeta:
        """返回点位进程级元数据；缺省时返回空元数据。"""
        return self._point_meta.get(point_id, PointMeta(variable_name=None, point_groups=()))

    def point_group_points(self, point_group: str) -> list[Point]:
        """返回属于指定 point_group 的点定义（诊断批量验证用）。"""
        return [
            point
            for point in self._point_table.points.values()
            if point_group in self.point_meta(point.point_id).point_groups
        ]

    async def connect(self) -> None:
        """建立底层协议连接。"""
        await self._protocol.connect()

    async def close(self) -> None:
        """关闭底层协议连接并释放资源；重复调用由 Driver 保证安全。"""
        await self._protocol.close()

    def is_open(self) -> bool:
        """底层协议传输连接当前是否打开（本地查询，不触发网络 I/O）。"""
        return self._protocol.is_open()

    def health(self) -> ConnectionHealth:
        """返回协议缓存的健康状态，不触发实时网络探测。"""
        return self._protocol.health()

    async def read_point(self, point_id: str) -> PointReading:
        """单点即时读取（独立公开入口，不经过批量路径）。

        Raises:
            CommandError: 点位未知。
            ProtocolError: 协议级读取失败。
        """
        self.point(point_id)
        sample = await self._protocol.read_one(point_id)
        return self._to_reading(sample)

    async def read_points(self, point_ids: list[str]) -> list[PointReading]:
        """按 point_id 批量读取并返回工程值，与请求逐位一一对应。

        协议批量结果必须满足 read_many 契约：数量与请求一致、按请求位置
        返回同名点样本（重复点按出现位置重复返回）。缺失、乱序、错位或
        多余样本直接以 ProtocolError 显式失败——不允许静默漏点，也不
        允许把别的点的值错配到请求点上。点级坏质量以 BAD 质量样本正常
        返回，不视为契约违约。

        Raises:
            CommandError: 任一点位未知。
            ProtocolError: 协议级读取失败或批量结果违反契约。
        """
        for point_id in point_ids:
            self.point(point_id)
        samples = await self._protocol.read_many(point_ids)
        validate_read_many_results(point_ids, samples)
        return [self._to_reading(sample) for sample in samples]

    def _to_reading(self, sample: ProtocolSample) -> PointReading:
        """把协议原始样本换算为工程值读取结果。"""
        point = self._point_table.points[sample.point_id]
        return PointReading(
            device_id=str(self._device.device_id),
            point_id=sample.point_id,
            value=engineering_value(point, sample.value),
            quality=sample.quality,
            timestamp=sample.timestamp,
            timestamp_source=sample.timestamp_source,
            source=self.protocol_name,
        )

    def protocol_write_value(self, point_id: str, value: WritableScalar) -> ProtocolWrite:
        """把业务写值转换为协议写值（含 PointAccess 检查与逆变换）。

        Raises:
            CommandError: 点未知或点不允许写入。
        """
        point = self.point(point_id)
        if point.access is PointAccess.READ:
            raise CommandError(f"point '{self._device.device_id}/{point_id}' is read-only")
        return ProtocolWrite(
            point_id=point_id,
            value=raw_write_value(point, value),
        )

    async def write_point(self, point_id: str, value: WritableScalar) -> None:
        """执行单点写入。

        Raises:
            CommandError: 点未知、只读或协议确认失败。
            ProtocolError: 协议级写入失败。
        """
        write = self.protocol_write_value(point_id, value)
        result = await self._protocol.write_one(write)
        if not result.success:
            raise CommandError(
                result.message or f"write rejected by device '{self._device.device_id}'"
            )
