"""AGC/AVC 子站指令模型端口。"""

from typing import Protocol

from seahorse.domain.models import DeviceSnapshot, SimulationTime


class DispatchStationPort(Protocol):
    def step(self, time: SimulationTime) -> None: ...
    def snapshot(self) -> DeviceSnapshot: ...
