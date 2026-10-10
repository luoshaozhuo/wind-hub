"""一次性配置快照：仅聚合已完成构造的领域实体。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.domain import (
    BusinessPoint,
    BusinessPointId,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    Site,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    PointMeta,
    PointTable,
    PointTableId,
    Task,
)
from core.domain.device import ProtocolOptions
from core.domain.config import SystemConfig
from core.application.sink_config import SinkConfig


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """共享领域配置快照；没有 YAML 包装类型。"""

    system: SystemConfig
    device_types: Mapping[DeviceTypeId, DeviceType]
    device_models: Mapping[DeviceModelId, DeviceModel]
    device_groups: Mapping[DeviceGroupId, DeviceGroup]
    site: Site
    point_tables: Mapping[PointTableId, PointTable]
    business_points: Mapping[BusinessPointId, BusinessPoint]
    tasks: Mapping[str, Task]
    sinks: Mapping[str, SinkConfig]
    protocol_options_by_device: Mapping[DeviceId, ProtocolOptions]
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]]

    def __post_init__(self) -> None:
        for name in (
            "device_types", "device_models", "device_groups",
            "point_tables", "business_points", "tasks", "sinks",
            "protocol_options_by_device",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(
            self, "point_meta",
            MappingProxyType({
                key: MappingProxyType(dict(value))
                for key, value in self.point_meta.items()
            }),
        )
