"""仿真时钟和设备快照的共享契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

Scalar: TypeAlias = float | int | bool


@dataclass(frozen=True, slots=True)
class SimulationTime:
    step_index: int
    seconds: float
    dt_seconds: float


@dataclass(frozen=True, slots=True)
class WindSample:
    speed_mps: float
    direction_deg: float


@dataclass(frozen=True, slots=True)
class GridSample:
    frequency_hz: float
    voltage_v: float
    phase_deg: float


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    device_id: str
    points: dict[str, Scalar]


@dataclass(frozen=True, slots=True)
class FarmSnapshot:
    time: SimulationTime
    devices: tuple[DeviceSnapshot, ...]

    def by_id(self, device_id: str) -> DeviceSnapshot:
        return next(device for device in self.devices if device.device_id == device_id)
