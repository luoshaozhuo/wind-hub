"""与配置源无关的类型化主题返回对象。

此处只定义数据契约。Schema 解析与具体配置格式由 Infrastructure 负责。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DeviceModelsConfig:
    """设备型号配置主题。"""

    definition: Any


@dataclass(frozen=True, slots=True)
class DeviceInstancesConfig:
    """设备实例配置主题。"""

    definition: Any


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    models: Any
    instances: Any


@dataclass(frozen=True, slots=True)
class PointConfig:
    tables: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: str
    device: str | None
    device_group: str | None
    point_group: str
    interval: float | None
    targets: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True, slots=True)
class TaskConfig:
    tasks: tuple[TaskDefinition, ...]


@dataclass(frozen=True, slots=True)
class SystemConfig:
    sections: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class UnitDefinition:
    symbol: str
    name: str | None


@dataclass(frozen=True, slots=True)
class UnitConfig:
    units: Mapping[str, UnitDefinition]
