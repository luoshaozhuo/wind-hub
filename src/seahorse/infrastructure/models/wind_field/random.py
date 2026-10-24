"""按仿真步更新、同一时刻读取不重复采样的风环境。"""

import random

from seahorse.domain.models import SimulationTime, WindSample


class RandomWindField:
    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)
        self._sample = WindSample(8.0, 270.0)

    def step(self, time: SimulationTime) -> None:
        self._sample = WindSample(self._rng.uniform(5, 12), self._rng.uniform(0, 360))

    def sample(self, device_id: str) -> WindSample:
        return self._sample
