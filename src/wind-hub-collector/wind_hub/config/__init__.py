"""Collector 配置层：YAML schema、加载、继承展开和运行时配置解析。"""

from wind_hub.config.device_resolver import resolve_devices
from wind_hub.config.loader import load_config
from wind_hub.config.point_table_resolver import resolve_point_tables
from wind_hub.config.schema import (
    CollectionTaskConfig,
    Config,
    DeviceInstancesConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    DeviceTypeConfig,
    PointTablesConfig,
    ResolvedPointTables,
    SiteConfig,
    SystemConfig,
    TasksConfig,
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
