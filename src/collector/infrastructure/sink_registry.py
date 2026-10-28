"""Collector registration of reusable Core sink implementations."""

from collector.application.sink_registry import SinkRegistry
from core.infrastructure.sink import GrpcSink, KafkaSink, ModbusSink, RedisSink, TDengineSink


def build_sink_registry() -> SinkRegistry:
    registry = SinkRegistry()
    registry.register("modbus", ModbusSink)
    registry.register("grpc", GrpcSink)
    registry.register("kafka", KafkaSink)
    registry.register("redis", RedisSink)
    registry.register("tdengine", TDengineSink)
    return registry
