"""可复现的电网边界条件；无潮流求解。"""

from __future__ import annotations

import math

from seahorse.domain.models import GridSample, SimulationTime


class SimpleGrid:
    def __init__(self, frequency_hz: float = 50.0, voltage_v: float = 35000.0) -> None:
        self._frequency = frequency_hz
        self._voltage = voltage_v
        self._sample = GridSample(frequency_hz, voltage_v, 0.0)

    def step(self, time: SimulationTime) -> None:
        self._sample = GridSample(
            self._frequency + 0.02 * math.sin(time.seconds / 30),
            self._voltage * (1 + 0.002 * math.sin(time.seconds / 60)),
            (360 * self._frequency * time.seconds) % 360,
        )

    def sample(self) -> GridSample:
        return self._sample
