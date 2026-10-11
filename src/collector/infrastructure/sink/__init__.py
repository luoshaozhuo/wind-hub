"""Collector Sink 组合入口：注册 File、Modbus、Redis 三种 Adapter。"""

from __future__ import annotations

from collector.application.sink_registry import SinkRegistry

from .file.csv import FileSink
from .modbus import ModbusSink
from .redis import RedisSink


def build_sink_registry() -> SinkRegistry:
    """显式注册内置 Sink 工厂，不依赖运行期按类型动态导入。"""
    registry = SinkRegistry()
    registry.register("file", FileSink)
    registry.register("modbus", ModbusSink)
    registry.register("redis", RedisSink)
    return registry


__all__ = ["build_sink_registry"]
