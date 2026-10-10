"""Collector Sink Adapter 组合入口：File、Modbus、Redis。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from collector.application.sink_port import SinkRegistry

if TYPE_CHECKING:
    from collector.application.sink_port import SinkPort
    from core.application.sink_config import ResolvedSinkConfig


def _file(config: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.file.csv import FileSink

    return FileSink(config)


def _modbus(config: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.modbus import ModbusSink

    return ModbusSink(config)


def _redis(config: ResolvedSinkConfig) -> SinkPort:
    from collector.infrastructure.sink.redis import RedisSink

    return RedisSink(config)


def build_sink_registry() -> SinkRegistry:
    """按需延迟导入具体 Adapter，不强迫 File-only 部署安装 Redis 或 Modbus。"""
    registry = SinkRegistry()
    registry.register("file", _file)
    registry.register("modbus", _modbus)
    registry.register("redis", _redis)
    return registry


__all__ = ["build_sink_registry"]
