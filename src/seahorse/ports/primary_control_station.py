"""一次调频及调压信号模型端口。"""

from typing import Protocol

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class PrimaryControlStationPort(Protocol):
    def step(self, time: SimulationTime, grid: GridSample) -> None: ...
    def snapshot(self) -> DeviceSnapshot: ...
