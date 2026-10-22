"""电网边界条件模型端口。"""
from typing import Protocol

from seahorse.domain.models import GridSample, SimulationTime


class GridPort(Protocol):
    def step(self, time: SimulationTime) -> None: ...
    def sample(self) -> GridSample: ...
