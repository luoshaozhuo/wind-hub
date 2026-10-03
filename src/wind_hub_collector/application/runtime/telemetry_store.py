"""Collector 真实采集数据的有界内存读模型。

本 Store 只接收 :class:`AcquisitionEngine` 已完成协议读取、工程值换算后的
`PointValue` 批次。主动轮询和协议订阅都会经过同一个 Observer，因此这里
保存的是 Collector 的真实采集结果，而不是管理页面触发的额外设备读取。
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime

from wind_hub_core.model.point import PointValue


class CollectorTelemetryStore:
    """保存最新点值与每点有界短期趋势。"""

    def __init__(self, max_samples_per_point: int = 3600) -> None:
        if max_samples_per_point <= 0:
            raise ValueError("max_samples_per_point must be greater than 0")
        self._max_samples = max_samples_per_point
        self._latest: dict[tuple[str, str], PointValue] = {}
        self._trend: dict[tuple[str, str], deque[PointValue]] = defaultdict(
            lambda: deque(maxlen=self._max_samples)
        )
        self._lock = threading.RLock()

    def observe_points(self, values: list[PointValue]) -> None:
        """接收 AcquisitionEngine Observer 的真实采集批次。"""
        with self._lock:
            for value in values:
                snapshot = value.model_copy(deep=True)
                key = (snapshot.device_id, snapshot.point_id)
                self._latest[key] = snapshot
                self._trend[key].append(snapshot)

    def latest_for_device(self, device_id: str) -> dict[str, PointValue]:
        """返回指定设备全部点的最新采集值独立快照。"""
        with self._lock:
            return {
                point_id: value.model_copy(deep=True)
                for (did, point_id), value in self._latest.items()
                if did == device_id
            }

    def trend_for_device(
        self,
        device_id: str,
        point_ids: set[str],
        *,
        since: datetime | None = None,
        limit_per_point: int = 600,
    ) -> dict[str, list[PointValue]]:
        """查询指定设备/点集合的真实采集短期趋势。"""
        if limit_per_point <= 0:
            raise ValueError("limit_per_point must be greater than 0")

        result: dict[str, list[PointValue]] = {}
        with self._lock:
            for point_id in sorted(point_ids):
                values = list(self._trend.get((device_id, point_id), ()))
                if since is not None:
                    values = [value for value in values if value.timestamp >= since]
                values = values[-limit_per_point:]
                result[point_id] = [
                    value.model_copy(deep=True)
                    for value in values
                ]
        return result
