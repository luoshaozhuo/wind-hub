"""Server 短期即时采样历史的进程内实现。"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime

from wind_hub_core.model.point import PointValue


class InMemoryTrendStore:
    """每个 device/point 使用有界 deque 的短期趋势缓存。"""

    def __init__(self, max_samples_per_point: int = 3600) -> None:
        if max_samples_per_point <= 0:
            raise ValueError("max_samples_per_point must be greater than 0")
        self._max_samples = max_samples_per_point
        self._series: dict[tuple[str, str], deque[PointValue]] = defaultdict(
            lambda: deque(maxlen=self._max_samples)
        )
        self._lock = threading.RLock()

    def append_batch(self, values: list[PointValue]) -> None:
        """按采集到达顺序追加样本，超过上限自动淘汰最旧值。

        PointValue 是不可变 Value Object，直接存储对象引用即可，
        调用方无法通过原引用篡改缓存内容。
        """
        with self._lock:
            for value in values:
                self._series[(value.device_id, value.point_id)].append(value)

    def query(
        self,
        device_id: str,
        point_ids: set[str],
        *,
        since: datetime | None = None,
        limit_per_point: int = 600,
    ) -> dict[str, list[PointValue]]:
        """查询短期趋势；每点最多返回 limit_per_point 个最近样本。

        锁内只取 deque 快照；since 过滤、limit 截断与结果构造在锁外完成。
        返回的 list 是独立容器，元素为共享的不可变 PointValue 引用。
        """
        if limit_per_point <= 0:
            raise ValueError("limit_per_point must be greater than 0")
        with self._lock:
            snapshots = {
                point_id: list(self._series.get((device_id, point_id), ()))
                for point_id in sorted(point_ids)
            }
        result: dict[str, list[PointValue]] = {}
        for point_id, values in snapshots.items():
            if since is not None:
                values = [value for value in values if value.timestamp >= since]
            result[point_id] = values[-limit_per_point:]
        return result
