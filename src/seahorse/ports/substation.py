"""升压站测量模型端口。"""
from typing import Protocol

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class SubstationPort(Protocol):
    def step(
        self, time: SimulationTime, turbines: tuple[DeviceSnapshot, ...], grid: GridSample
    ) -> None: ...
    def snapshot(self) -> DeviceSnapshot: ...
