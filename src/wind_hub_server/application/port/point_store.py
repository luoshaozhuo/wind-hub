"""Server 短期即时采样历史存储端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from wind_hub_core.model.point import PointValue


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
