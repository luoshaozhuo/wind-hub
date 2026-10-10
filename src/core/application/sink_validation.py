"""独立的新 Sink 配置集合校验（不触碰现有配置服务）。"""

from __future__ import annotations

from collections.abc import Sequence

from core.application.sink_contract import (
    FileSinkConfig,
    ModbusSinkConfig,
    RedisSinkConfig,
)

SinkDefinition = FileSinkConfig | ModbusSinkConfig | RedisSinkConfig


def validate_sink_definitions(sinks: Sequence[SinkDefinition]) -> None:
    """检查跨 Sink 的唯一名称及监听资源冲突。"""

    names: set[str] = set()
    listeners: set[tuple[str, int]] = set()
    for sink in sinks:
        name = sink.name.strip()
        if not name:
            raise ValueError("sink name must not be empty")
        if name in names:
            raise ValueError(f"duplicate sink name: {name}")
        names.add(name)
        if not sink.enabled or not isinstance(sink, ModbusSinkConfig):
            continue
        key = (sink.connection.host, sink.connection.port)
        if key in listeners:
            raise ValueError(f"duplicate Modbus listener: {key}")
        listeners.add(key)
