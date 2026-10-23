"""primary 随机指令信号适配器；不负责 EMS 控制律。"""

from __future__ import annotations

import random
from collections.abc import Mapping

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class RandomPrimaryControlStation:
    def __init__(self, seed: int, points: Mapping[str, tuple[float, float]]) -> None:
        self._rng = random.Random(seed)
        self._points = dict(points)
        self._snapshot = DeviceSnapshot("primary", {})

    def step(self, time: SimulationTime, grid: GridSample) -> None:
        self._snapshot = DeviceSnapshot(
            "primary",
            {point: self._rng.uniform(*bounds) for point, bounds in self._points.items()},
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot
