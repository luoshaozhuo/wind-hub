"""Collector registration of reusable Core sink implementations."""

from collector.application.sink_registry import SinkRegistry
from core.infrastructure.sink import FileSink, ModbusSink, RedisSink


def build_sink_registry() -> SinkRegistry:
    registry = SinkRegistry()
    registry.register("file", FileSink)
    registry.register("modbus", ModbusSink)
    registry.register("redis", RedisSink)
    return registry
