"""随机电网边界条件；不执行潮流计算。"""
import random

from seahorse.domain.models import GridSample, SimulationTime


class RandomGrid:
    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)
        self._sample = GridSample(50.0, 35000.0, 0.0)

    def step(self, time: SimulationTime) -> None:
        self._sample = GridSample(self._rng.uniform(49.9, 50.1), 35000.0, 0.0)

    def sample(self) -> GridSample:
        return self._sample
