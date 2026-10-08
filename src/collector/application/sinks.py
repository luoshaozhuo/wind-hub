"""兼容 Collector 旧导入路径；Sink 契约的唯一实现在 Core。"""

from core.application.sink_config import (
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
    SinkAddress,
    SinkConfig,
    SinkPoint,
    SinkSource,
    SinksConfig,
    StreamSinkAddress,
)

__all__ = [
    "SINK_TYPES", "SINK_DATA_TYPES", "SINK_NUMERIC_DATA_TYPES",
    "MODBUS_WORD_WIDTH", "SinkSource", "FileSinkConnection",
    "KafkaSinkConnection", "DatabaseSinkConnection", "IEC104SinkConnection",
    "OPCUASinkConnection", "ModbusSinkConnection", "StreamSinkAddress",
    "IEC104SinkAddress", "OPCUASinkAddress", "ModbusSinkAddress",
    "SinkAddress", "SinkPoint", "ResolvedSinkPoint", "SinkConfig",
    "ResolvedSinkConfig", "SinksConfig",
]
