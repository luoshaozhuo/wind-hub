"""兼容 Collector 旧导入路径；Sink 契约的唯一实现在 Core。"""

from core.application.sink_config import *  # noqa: F403

__all__ = [
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
    "SinkAddress",
    "SinkPoint",
    "ResolvedSinkPoint",
    "SinkConfig",
    "ResolvedSinkConfig",
    "SinksConfig",
]
