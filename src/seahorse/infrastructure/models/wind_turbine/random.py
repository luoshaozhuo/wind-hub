"""按配置测点类型生成随机数据，控制值不等于实际功率。"""

from __future__ import annotations

import random
import struct
from collections.abc import Mapping

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import DeviceSnapshot, GridSample, Scalar, SimulationTime, WindSample
from seahorse.infrastructure.config import RandomPoint


class RandomTurbine:
    def __init__(self, device_id: str, seed: int, points: Mapping[str, RandomPoint]) -> None:
        self._id = device_id
        self._rng = random.Random(seed)
        self._points = dict(points)
        self._values: dict[str, Scalar] = {}
        self._controls: dict[str, Scalar] = {}

    @property
    def device_id(self) -> str:
        return self._id

    def apply(self, command: EquipmentCommand) -> None:
        if command.point_id not in self._points:
            raise ValueError(f"unsupported point: {command.point_id}")
        point = self._points[command.point_id]
        value = command.value
        if point.data_type == "bool":
            valid = type(value) is bool
        elif point.data_type.startswith(("int", "uint")):
            valid = type(value) is int and point.minimum <= value <= point.maximum
        else:
            valid = type(value) is float and point.minimum <= value <= point.maximum
        if not valid:
            raise ValueError("command value violates type or range")
        self._controls[command.point_id] = value
        self._values[command.point_id] = value

    def step(self, time: SimulationTime, wind: WindSample, grid: GridSample) -> None:
        values: dict[str, Scalar] = {}
        for point_id, point in self._points.items():
            if point_id in self._controls:
                values[point_id] = self._controls[point_id]
            elif point.data_type == "bool":
                values[point_id] = bool(self._rng.getrandbits(1))
            elif point.data_type.startswith(("int", "uint")):
                values[point_id] = self._rng.randint(int(point.minimum), int(point.maximum))
            else:
                weight = self._rng.random()
                value = float(point.minimum) * (1.0 - weight) + float(point.maximum) * weight
                if point.data_type == "float32":
                    value = struct.unpack(">f", struct.pack(">f", value))[0]
                values[point_id] = value
        self._values = values

    def snapshot(self) -> DeviceSnapshot:
        return DeviceSnapshot(self._id, dict(self._values))
