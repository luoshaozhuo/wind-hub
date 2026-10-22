"""独立于工业协议的设备控制命令。"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Scalar


@dataclass(frozen=True, slots=True)
class EquipmentCommand:
    command_id: str
    device_id: str
    point_id: str
    value: Scalar
