"""控制回读 Latest/Trend Store 的进程内实现。"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime

from wind_hub_core.model.point import PointValue


class InMemoryControlReadbackLatestStore:
    """线程安全的 Commander 控制回读最新值缓存。"""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], PointValue] = {}
        self._lock = threading.RLock()

    def put_batch(self, values: list[PointValue]) -> None:
        """仅保留每个 device/point 最近一次收到的值。"""
        with self._lock:
            for value in values:
                self._values[(value.device_id, value.point_id)] = value.model_copy(deep=True)

    def get(self, device_id: str, point_id: str) -> PointValue | None:
        """返回独立快照，防止调用方修改缓存对象。"""
        with self._lock:
            value = self._values.get((device_id, point_id))
            return value.model_copy(deep=True) if value is not None else None

    def list_device(self, device_id: str) -> dict[str, PointValue]:
        """返回指定设备当前全部最新值。"""
        with self._lock:
            return {
                point_id: value.model_copy(deep=True)
                for (did, point_id), value in self._values.items()
                if did == device_id
            }


class InMemoryControlReadbackControlReadbackTrendStore:
    """每个 device/point 使用有界 deque 的控制回读短期趋势缓存。"""

    def __init__(self, max_samples_per_point: int = 3600) -> None:
        if max_samples_per_point <= 0:
            raise ValueError("max_samples_per_point must be greater than 0")
        self._max_samples = max_samples_per_point
        self._series: dict[tuple[str, str], deque[PointValue]] = defaultdict(
            lambda: deque(maxlen=self._max_samples)
        )
        self._lock = threading.RLock()

    def append_batch(self, values: list[PointValue]) -> None:
        """按采集到达顺序追加样本，超过上限自动淘汰最旧值。"""
        with self._lock:
            for value in values:
                self._series[(value.device_id, value.point_id)].append(
                    value.model_copy(deep=True)
                )

    def query(
        self,
        device_id: str,
        point_ids: set[str],
        *,
        since: datetime | None = None,
        limit_per_point: int = 600,
    ) -> dict[str, list[PointValue]]:
        """查询短期趋势；每点最多返回 limit_per_point 个最近样本。"""
        if limit_per_point <= 0:
            raise ValueError("limit_per_point must be greater than 0")
        result: dict[str, list[PointValue]] = {}
        with self._lock:
            for point_id in sorted(point_ids):
                values = list(self._series.get((device_id, point_id), ()))
                if since is not None:
                    values = [value for value in values if value.timestamp >= since]
                values = values[-limit_per_point:]
                result[point_id] = [value.model_copy(deep=True) for value in values]
        return result
