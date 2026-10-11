"""Collector 最近采集值快照；仅缓存当前设备/测点最后一次上报。"""

from __future__ import annotations

from core.domain.point_value import PointValue


class LatestValues:
    """以 (device_id, point_id) 为键保存最近值，不参与采集或 Sink 派发。"""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], PointValue] = {}

    def update(self, values: list[PointValue]) -> None:
        for value in values:
            self._values[(value.device_id, value.point_id)] = value

    def snapshot(self, device_id: str | None = None) -> list[PointValue]:
        return [
            value
            for (current_device, _), value in sorted(self._values.items())
            if device_id is None or current_device == device_id
        ]

    def clear_device(self, device_id: str) -> None:
        for key in tuple(self._values):
            if key[0] == device_id:
                del self._values[key]

    def clear(self) -> None:
        self._values.clear()
