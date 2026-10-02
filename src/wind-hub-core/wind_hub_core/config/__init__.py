"""Wind Hub 跨进程共享配置模型与纯解析能力。"""

from wind_hub_core.config.device_resolver import resolve_devices
from wind_hub_core.config.point_table_resolver import resolve_point_tables
from wind_hub_core.config.schema import (
    ADSSystemConfig,
    DeviceConfig,
    DeviceInstanceConfig,
    DeviceInstancesConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    DeviceTypeConfig,
    InstanceEndpoint,
    PointAddress,
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
    ResolvedPointTable,
    ResolvedPointTables,
    SiteConfig,
    UnitConfig,
    UnitsConfig,
)

__all__ = [
    "ADSSystemConfig",
    "SiteConfig",
    "UnitConfig",
    "UnitsConfig",
    "DeviceTypeConfig",
    "DeviceModelConfig",
    "DeviceModelsConfig",
    "InstanceEndpoint",
    "DeviceInstanceConfig",
    "DeviceInstancesConfig",
    "DeviceConfig",
    "DevicesConfig",
    "PointAddress",
    "PointConfig",
    "PointPatch",
    "PointTableConfig",
    "PointTablesConfig",
    "ResolvedPointTable",
    "ResolvedPointTables",
    "resolve_devices",
    "resolve_point_tables",
]
