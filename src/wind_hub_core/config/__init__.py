"""Wind Hub 跨进程共享配置的稳定 public façade。

生产代码统一经本包访问配置领域模型（``config.model``）、解析
（``config.resolver``）、加载（``config.loader``）、差异与指纹，不依赖
内部物理文件路径。``__all__`` 显式维护全部导出。
"""

from wind_hub_core.config.diff import compute_diff
from wind_hub_core.config.fingerprint import fingerprint_config_set
from wind_hub_core.config.loader import load_config
from wind_hub_core.config.model.config import Config
from wind_hub_core.config.model.device import (
    DeviceConfig,
    DeviceInstanceConfig,
    DeviceInstancesConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    DeviceTypeConfig,
    InstanceEndpoint,
)
from wind_hub_core.config.model.point import (
    PointAddress,
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
    ResolvedPointTable,
    ResolvedPointTables,
)
from wind_hub_core.config.model.protocol import SUPPORTED_PROTOCOLS
from wind_hub_core.config.model.sink import (
    MODBUS_WORD_WIDTH,
    SINK_DATA_TYPES,
    SINK_NUMERIC_DATA_TYPES,
    SINK_TYPES,
    DatabaseSinkConnection,
    FileSinkConnection,
    IEC104SinkAddress,
    IEC104SinkConnection,
    KafkaSinkConnection,
    ModbusSinkAddress,
    ModbusSinkConnection,
    OPCUASinkAddress,
    OPCUASinkConnection,
    ResolvedSinkConfig,
    ResolvedSinkPoint,
    ResolvedSinksConfig,
    SinkConfig,
    SinkPoint,
    SinksConfig,
    SinkSource,
    StreamSinkAddress,
)
from wind_hub_core.config.model.system import (
    ADSSystemConfig,
    ApiConfig,
    InterfaceConfig,
    RuntimeConfig,
    SiteConfig,
    SystemConfig,
)
from wind_hub_core.config.model.task import (
    CollectionTaskConfig,
    TasksConfig,
    TaskTarget,
)
from wind_hub_core.config.model.unit import UnitConfig, UnitsConfig
from wind_hub_core.config.resolver.device import resolve_devices
from wind_hub_core.config.resolver.point_table import resolve_point_tables
from wind_hub_core.config.resolver.sink import resolve_sinks

__all__ = [
    "ADSSystemConfig",
    "SiteConfig",
    "RuntimeConfig",
    "ApiConfig",
    "InterfaceConfig",
    "SystemConfig",
    "UnitConfig",
    "UnitsConfig",
    "SUPPORTED_PROTOCOLS",
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
    "TaskTarget",
    "CollectionTaskConfig",
    "TasksConfig",
    "SINK_TYPES",
    "SINK_DATA_TYPES",
    "SINK_NUMERIC_DATA_TYPES",
    "MODBUS_WORD_WIDTH",
    "SinkSource",
    "FileSinkConnection",
    "KafkaSinkConnection",
    "DatabaseSinkConnection",
    "IEC104SinkConnection",
    "OPCUASinkConnection",
    "ModbusSinkConnection",
    "StreamSinkAddress",
    "IEC104SinkAddress",
    "OPCUASinkAddress",
    "ModbusSinkAddress",
    "SinkPoint",
    "ResolvedSinkPoint",
    "SinkConfig",
    "ResolvedSinkConfig",
    "SinksConfig",
    "ResolvedSinksConfig",
    "Config",
    "resolve_devices",
    "resolve_point_tables",
    "resolve_sinks",
    "load_config",
    "fingerprint_config_set",
    "compute_diff",
]
