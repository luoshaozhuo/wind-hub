"""Collector 最近采集值快照；仅缓存当前设备/测点最后一次上报。

更新规则按采样时间戳（sample time）：到达的采样若时间戳早于已缓存
采样则丢弃，保证乱序/迟到上报不会覆盖较新值；时间戳相同则后来者
覆盖（同采样的重复派发以最后一次到达为准）。

缓存键集合被配置界定：配置激活时 ``retain`` 记录新点表的有效键并
裁剪缓存；此后迟到回调（如旧订阅在关闭前已在途的上送）携带的已
删除设备/测点不会重新进入缓存，缓存不会无限增长。
"""

from __future__ import annotations

from collections.abc import Mapping

from core.domain.point_value import PointValue


class LatestValues:
    """以 (device_id, point_id) 为键保存最近值，不参与采集或 Sink 派发。

    所有方法只在事件循环内调用（采集回调与 gRPC 查询同属一个 loop），
    dict 操作原子，无需额外锁。
    """

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], PointValue] = {}
        # None 表示尚未经历过配置激活——初始配置即当前基线，不做键约束。
        self._valid_points: Mapping[str, frozenset[str]] | None = None

    def update(self, values: list[PointValue]) -> None:
        """按采样时间更新：早于已缓存采样时间戳的上报被丢弃。"""
        for value in values:
            if self._valid_points is not None and value.point_id not in self._valid_points.get(
                value.device_id, frozenset()
            ):
                # 配置激活后迟到回调携带的已删除测点——直接丢弃。
                continue
            key = (value.device_id, value.point_id)
            current = self._values.get(key)
            if current is not None and value.timestamp < current.timestamp:
                continue
            self._values[key] = value

    def snapshot(self, device_id: str | None = None) -> list[PointValue]:
        """返回当前缓存快照；可按设备过滤，结果按 (device_id, point_id) 排序。"""
        return [
            value
            for (current_device, _), value in sorted(self._values.items())
            if device_id is None or current_device == device_id
        ]

    def retain(self, valid_points: Mapping[str, frozenset[str]]) -> None:
        """按有效「设备 → 测点集合」约束并裁剪缓存（配置激活后调用）。

        已删除设备、已删除测点与点表变更后不再存在的键全部清除；
        仍存在的键保留真实采样时间戳，不伪造新数据。之后的 ``update``
        只接受本有效键集合内的测点。
        """
        self._valid_points = {device: frozenset(points) for device, points in valid_points.items()}
        for device_id, point_id in tuple(self._values):
            if point_id not in self._valid_points.get(device_id, frozenset()):
                del self._values[(device_id, point_id)]

    def clear(self) -> None:
        """清空全部缓存。"""
        self._values.clear()
