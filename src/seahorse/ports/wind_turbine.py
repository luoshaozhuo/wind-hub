"""风机运行和控制端口。"""

from typing import Protocol

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime, WindSample


class WindTurbinePort(Protocol):
    @property
    def device_id(self) -> str: ...
    def apply(self, command: EquipmentCommand) -> None: ...
    def step(self, time: SimulationTime, wind: WindSample, grid: GridSample) -> None: ...
    def snapshot(self) -> DeviceSnapshot: ...
