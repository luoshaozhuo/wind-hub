"""协议主动探测端口。

具体 ADS/Modbus/IEC104 实现属于协议基础设施；Server 和 Collector 只依赖
本接口，不直接依赖彼此的进程代码。
"""

from __future__ import annotations

from typing import Protocol

from .models import AddressResolution, DeviceProbeTarget, PointProbeSpec


class ProtocolProbe(Protocol):
    """协议主动探测的最小公共接口。

    一个实例代表一次短生命周期验证 session。调用方负责 connect / close；
    实现必须避免在已连接状态重复建立远端连接。
    """

    @property
    def connected(self) -> bool:
        """当前 probe session 是否已建立协议连接。"""
        ...

    async def connect(self) -> None:
        """建立协议连接；已连接时必须幂等返回。"""
        ...

    async def close(self) -> None:
        """释放 probe session。"""
        ...

    async def resolve_points(
        self,
        points: list[PointProbeSpec],
    ) -> dict[str, AddressResolution]:
        """解析点地址；不支持解析的协议可返回原配置地址快照。"""
        ...

    async def verify_read(
        self,
        points: list[PointProbeSpec],
        resolutions: dict[str, AddressResolution],
    ) -> set[str]:
        """验证点可读性，返回读取成功的 point_id 集合。"""
        ...
