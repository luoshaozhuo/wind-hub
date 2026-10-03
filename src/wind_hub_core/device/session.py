"""跨进程共享的设备通信会话。

DeviceSession 聚合单台设备的静态配置、resolved 点表与 ProtocolPort，统一提供
连接、健康检查、按点读取、按组读取、写入以及 scale/offset 工程值换算。它不负责
周期调度、Task 生命周期、采集统计或 Sink 派发；这些属于 Collector。
"""

from __future__ import annotations

from wind_hub_core.config.schema import DeviceConfig, PointConfig
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointRef, PointValue
from wind_hub_core.protocol.port import AcquisitionMode, ProtocolPort


class DeviceSession:
    """单台设备的通信会话聚合。

    Args:
        config: resolved 设备配置。
        points: 当前设备 resolved 点表。
        protocol: 已按 DeviceConfig 创建的协议 Driver。

    Notes:
        本对象不主动建立连接；调用方显式执行 connect/close。构造时立即把当前
        resolved 点表注入 Driver，以保证会话一旦创建即可安全 read/write；后续
        轻量热更新通过 set_points 同步重建寻址映射。
    """

    def __init__(
        self,
        config: DeviceConfig,
        points: list[PointConfig],
        protocol: ProtocolPort,
    ) -> None:
        self._config = config
        self._points = tuple(points)
        self._protocol = protocol
        self._protocol.set_points_mapping(list(self._points))

    @property
    def config(self) -> DeviceConfig:
        """返回当前设备配置快照。"""
        return self._config

    @config.setter
    def config(self, value: DeviceConfig) -> None:
        """替换不要求重建协议实例的设备配置快照。"""
        self._config = value

    @property
    def device_id(self) -> str:
        """返回设备稳定标识。"""
        return self._config.device_id

    @property
    def device_group(self) -> str | None:
        """返回设备分组。"""
        return self._config.device_group

    @property
    def enabled(self) -> bool:
        """返回配置级启用状态。"""
        return self._config.enabled

    @property
    def protocol(self) -> ProtocolPort:
        """返回当前协议运行实例。"""
        return self._protocol

    @property
    def points(self) -> tuple[PointConfig, ...]:
        """返回当前 resolved 点表不可变快照。"""
        return self._points

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """返回协议声明的持续采集能力。"""
        return self._protocol.acquisition_mode

    def set_points(self, points: list[PointConfig]) -> None:
        """更新点表并同步重建协议寻址映射。

        Args:
            points: 新的 resolved 点表。

        Notes:
            本操作只更新内存映射，不建立或关闭协议连接。
        """
        self._points = tuple(points)
        self._protocol.set_points_mapping(list(self._points))

    def point_group_points(self, point_group: str) -> list[PointConfig]:
        """返回属于指定 point_group 的点定义。

        Args:
            point_group: 点分组名称。

        Returns:
            point_groups 含该名称的 PointConfig 列表。
        """
        return [point for point in self._points if point_group in point.point_groups]

    def point_refs(self, point_group: str) -> list[PointRef]:
        """构造指定点组的批量读取引用。

        Args:
            point_group: 点分组名称。

        Returns:
            按当前点表顺序生成的 PointRef 列表。
        """
        return [
            PointRef(device_id=self.device_id, point_id=point.point_id)
            for point in self.point_group_points(point_group)
        ]

    def _normalize_values(self, values: list[PointValue]) -> list[PointValue]:
        """按点表 scale/offset 把协议原始值换算为工程值。

        只转换 int/float 且排除 bool；None、字符串和未知点原样保留，质量、时间戳
        与 source 不变。
        """
        by_id = {point.point_id: point for point in self._points}
        normalized: list[PointValue] = []
        for value in values:
            point = by_id.get(value.point_id)
            if (
                point is None
                or (point.scale == 1.0 and point.offset == 0.0)
                or not isinstance(value.value, int | float)
                or isinstance(value.value, bool)
            ):
                normalized.append(value)
                continue
            normalized.append(
                value.model_copy(
                    update={"value": value.value * point.scale + point.offset}
                )
            )
        return normalized

    async def connect(self) -> None:
        """建立底层协议连接。

        Raises:
            ProtocolError: Driver 建连或握手失败。
        """
        await self._protocol.connect()

    async def close(self) -> None:
        """关闭底层协议连接并释放其资源；重复调用由 Driver 保证安全。"""
        await self._protocol.close()

    def health(self) -> HealthStatus:
        """返回协议缓存的健康状态，不触发实时网络探测。"""
        return self._protocol.health()

    async def read(self, point_group: str) -> list[PointValue]:
        """读取指定点组并返回已应用 scale/offset 的工程值。

        Args:
            point_group: 点分组名称。

        Returns:
            协议返回顺序对应的工程值列表。
        """
        return self._normalize_values(
            await self._protocol.read(self.point_refs(point_group))
        )

    async def read_points(self, refs: list[PointRef]) -> list[PointValue]:
        """按显式引用批量读取并返回工程值。

        Args:
            refs: 待读取点引用。

        Returns:
            已应用 scale/offset 的 PointValue 列表。
        """
        return self._normalize_values(await self._protocol.read(refs))

    async def write(self, commands: list[Command]) -> list[CommandResult]:
        """把设备写命令直接委托给协议 Driver。

        Args:
            commands: 待执行命令。

        Returns:
            与 Driver 返回顺序一致的 CommandResult 列表。
        """
        return await self._protocol.write(commands)
