"""惯量响应信号模型端口。"""
from typing import Protocol

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class InertiaStationPort(Protocol):
    def step(self, time: SimulationTime, grid: GridSample) -> None: ...
    def snapshot(self) -> DeviceSnapshot: ...
