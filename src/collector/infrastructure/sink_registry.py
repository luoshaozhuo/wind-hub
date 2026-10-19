"""Collector registration of reusable Core sink implementations."""

from collector.application.sink_registry import SinkRegistry
from core.infrastructure.sink import ModbusSink, RedisSink, TDengineSink


def build_sink_registry() -> SinkRegistry:
    registry = SinkRegistry()
    registry.register("modbus", ModbusSink)
    registry.register("redis", RedisSink)
    registry.register("tdengine", TDengineSink)
    return registry
