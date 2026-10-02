"""Collector 配置入口。

公共设备、点表和单位模型以及纯 Resolver 来自 wind-hub-core；本包仅聚合
Collector 专属配置和 YAML 加载入口。
"""

from wind_hub.config.loader import load_config
from wind_hub.config.schema import CollectionTaskConfig, Config, SystemConfig, TasksConfig
from wind_hub_core.config.device_resolver import resolve_devices
from wind_hub_core.config.point_table_resolver import resolve_point_tables
from wind_hub_core.config.schema import (
    DeviceInstancesConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    DeviceTypeConfig,
    PointTablesConfig,
    ResolvedPointTables,
    SiteConfig,
)

__all__ = [
    "CollectionTaskConfig",
    "Config",
    "SystemConfig",
    "SiteConfig",
    "DevicesConfig",
    "DeviceInstancesConfig",
    "DeviceTypeConfig",
    "DeviceModelConfig",
    "DeviceModelsConfig",
    "PointTablesConfig",
    "ResolvedPointTables",
    "TasksConfig",
    "load_config",
    "resolve_devices",
    "resolve_point_tables",
]
