"""三类 Sink 的跨实例唯一性与监听冲突检查。"""

from __future__ import annotations

from collections.abc import Sequence

from core.application.sink_contract import FileSinkConfig, ModbusSinkConfig, RedisSinkConfig

SinkDefinition = FileSinkConfig | ModbusSinkConfig | RedisSinkConfig


def validate_sink_definitions(sinks: Sequence[SinkDefinition]) -> None:
    """名称不能重复，同一端口不能绑定冲突的 Modbus 监听地址。"""
    names: set[str] = set()
    listeners: set[tuple[str, int]] = set()
    for sink in sinks:
        name = sink.name.strip()
        if not name or name in names:
            raise ValueError(f"empty or duplicate sink name: {sink.name}")
        names.add(name)
        if not sink.enabled or not isinstance(sink, ModbusSinkConfig):
            continue
        host, port = sink.connection.host, sink.connection.port
        if not host.strip():
            raise ValueError("Modbus listener host must not be empty")
        if any(
            existing_port == port
            and (existing_host == host or existing_host in ("0.0.0.0", "::")
                 or host in ("0.0.0.0", "::"))
            for existing_host, existing_port in listeners
        ):
            raise ValueError(f"duplicate Modbus listener: {(host, port)}")
        listeners.add((host, port))
