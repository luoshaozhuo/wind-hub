"""一次性配置快照：仅聚合已完成构造的领域实体。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from dataclasses import dataclass, field
from types import MappingProxyType

from core.domain import (
    BusinessPoint,
    BusinessPointId,
    DeviceGroup,
    DeviceGroupId,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    PointTable,
    PointTableId,
    Sink,
    Site,
    Task,
)
from core.domain.unit import UnitCatalog

from .settings import SystemSettings


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """共享领域配置快照；没有 YAML 包装类型。"""

    system: SystemSettings
    units: UnitCatalog
    device_types: Mapping[DeviceTypeId, DeviceType]
    device_models: Mapping[DeviceModelId, DeviceModel]
    device_groups: Mapping[DeviceGroupId, DeviceGroup]
    site: Site
    point_tables: Mapping[PointTableId, PointTable]
    business_points: Mapping[BusinessPointId, BusinessPoint]
    tasks: Mapping[str, Task]
    sinks: Mapping[str, Sink]
    # 原始点表声明仅供配置写回，不参与领域点解析和运行时访问。
    point_table_definitions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    point_table_baseline: Mapping[PointTableId, PointTable] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "device_types",
            "device_models",
            "device_groups",
            "point_tables",
            "business_points",
            "tasks",
            "sinks",
            "point_table_definitions",
            "point_table_baseline",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
