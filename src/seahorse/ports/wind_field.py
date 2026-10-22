"""风场环境模型端口。"""
from typing import Protocol

from seahorse.domain.models import SimulationTime, WindSample


class WindFieldPort(Protocol):
    def step(self, time: SimulationTime) -> None: ...
    def sample(self, device_id: str) -> WindSample: ...
