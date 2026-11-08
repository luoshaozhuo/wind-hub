"""根据持久化 Task placement 把即时读点定位到真实 Collector。"""

from __future__ import annotations

from typing import Protocol

from web_server.application.device.sample import PointValue
from web_server.application.port.collector_directory import CollectorDirectory
from web_server.application.task.collector import verified_collector
from web_server.application.task.placement import TaskPlacementRegistry


class DeviceReadPort(Protocol):
    """设备实时读点边界：必须返回实际协议采样，不接受缓存伪造。"""

    async def read_points(self, device_id: str, point_ids: list[str]) -> list[PointValue]: ...

    async def read_point(self, device_id: str, point_id: str) -> PointValue: ...


class CollectorReadRouter:
    """只有一个确定的 Collector Owner 时才允许设备即时读点。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        placements: TaskPlacementRegistry,
    ) -> None:
        self._collectors = collectors
        self._placements = placements

    async def read_points(self, device_id: str, point_ids: list[str]) -> list[PointValue]:
        if not point_ids:
            return []
        owners = self._placements.worker_ids_for_device(device_id)
        if len(owners) != 1:
            raise RuntimeError(
                f"device {device_id!r} requires exactly one readable Collector owner; "
                f"got {len(owners)}"
            )
        collector = await verified_collector(self._collectors, owners[0])
        return await collector.read_points(device_id, point_ids)

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        rows = await self.read_points(device_id, [point_id])
        if len(rows) != 1:
            raise RuntimeError("Collector did not return exactly one point")
        return rows[0]
