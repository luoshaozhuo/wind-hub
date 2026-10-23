"""按配置点生成随机风机数据，命令回读不等于实际功率。"""

from __future__ import annotations

import random
from collections.abc import Mapping

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import DeviceSnapshot, GridSample, Scalar, SimulationTime, WindSample


class RandomTurbine:
    def __init__(
        self, device_id: str, seed: int, points: Mapping[str, tuple[float, float]]
    ) -> None:
        self._id = device_id
        self._rng = random.Random(seed)
        self._points = dict(points)
        self._values: dict[str, Scalar] = {}
        self._controls: dict[str, Scalar] = {}

    @property
    def device_id(self) -> str:
        return self._id

    def apply(self, command: EquipmentCommand) -> None:
        if command.point_id not in self._points:
            raise ValueError(f"unsupported point: {command.point_id}")
        self._controls[command.point_id] = command.value
        self._values[command.point_id] = command.value

    def step(self, time: SimulationTime, wind: WindSample, grid: GridSample) -> None:
        self._values = {
            point_id: self._controls[point_id]
            if point_id in self._controls
            else self._rng.uniform(*bounds)
            for point_id, bounds in self._points.items()
        }

    def snapshot(self) -> DeviceSnapshot:
        return DeviceSnapshot(self._id, dict(self._values))
