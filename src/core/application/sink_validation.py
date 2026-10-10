"""Sink 实例间的监听资源冲突校验。"""

from __future__ import annotations

from collections.abc import Sequence

from core.application.sink_config import ModbusSinkConnection, SinkConfig


def validate_sink_definitions(sinks: Sequence[SinkConfig]) -> None:
    """检查名称重复及 Modbus TCP 端口的绑定冲突。"""
    names: set[str] = set()
    listeners: set[tuple[str, int]] = set()
    for sink in sinks:
        name = sink.name.strip()
        if not name or name in names:
            raise ValueError(f"empty or duplicate sink name: {sink.name}")
        names.add(name)
        if not sink.enabled or not isinstance(sink.connection, ModbusSinkConnection):
            continue
        host = sink.connection.host
        port = sink.connection.port
        if any(
            used_port == port
            and (used_host == host or used_host in ("0.0.0.0", "::")
                 or host in ("0.0.0.0", "::"))
            for used_host, used_port in listeners
        ):
            raise ValueError(f"duplicate Modbus listener: {(host, port)}")
        listeners.add((host, port))
