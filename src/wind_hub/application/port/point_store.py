"""最新值与短期趋势存储端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from wind_hub.domain.model.point import PointValue


class LatestPointStore(Protocol):
    """设备最新工程值缓存端口。"""

    def put_batch(self, values: list[PointValue]) -> None:
        """用批次中的值覆盖对应 device/point 的最新值。"""
        ...

    def get(self, device_id: str, point_id: str) -> PointValue | None:
        """读取单点最新值。"""
        ...

    def list_device(self, device_id: str) -> dict[str, PointValue]:
        """返回设备当前全部最新值，以 point_id 为键。"""
        ...


class TrendStore(Protocol):
    """短期趋势缓存端口。"""

    def append_batch(self, values: list[PointValue]) -> None:
        """按点追加采样。"""
        ...

    def query(
        self,
        device_id: str,
        point_ids: set[str],
        *,
        since: datetime | None = None,
        limit_per_point: int = 600,
    ) -> dict[str, list[PointValue]]:
        """按设备和点集查询短期样本。"""
        ...
