"""Sink adapters — 数据输出。

内置 Sink 注册的唯一入口是 :func:`build_sink_registry`；新增 Sink 类型时在
本函数登记一行 factory 即可，不存在中央 if/elif 分派链。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from collector.application.sink_port import SinkRegistry

if TYPE_CHECKING:
    from collector.application.sink_port import SinkPort
    from collector.application.sinks import ResolvedSinkConfig


def _file(cfg: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.file.csv import FileSink

    return FileSink(cfg)


def _kafka(cfg: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.mq.kafka import KafkaSink

    return KafkaSink(cfg)


def _db(cfg: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.db.postgres import DBSink

    return DBSink(cfg)


def _iec104(cfg: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.iec104 import IEC104Sink

    return IEC104Sink(cfg)


def _modbus(cfg: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.modbus import ModbusSink

    return ModbusSink(cfg)


def build_sink_registry() -> SinkRegistry:
    """构造注册好全部内置 Sink 类型的注册表。

    可选 Sink 依赖保持 lazy import——各 factory 只在配置实际使用该类型时才
    导入实现模块，File-only 部署无需安装 aiokafka/asyncpg。
    """
    registry = SinkRegistry()
    registry.register("file", _file)
    registry.register("kafka", _kafka)
    registry.register("db", _db)
    registry.register("iec104", _iec104)
    registry.register("modbus", _modbus)
    return registry


__all__ = ["build_sink_registry"]
