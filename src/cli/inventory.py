"""从已验证的配置快照查询对象；不访问现场设备。"""

from __future__ import annotations

from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.infrastructure.config.effective import resolve_effective_device


def devices(snapshot: ConfigSnapshot, device_id: str | None = None) -> list[dict[str, Any]]:
    """设备清单及实际生效的 Endpoint/协议参数。"""
    result: list[dict[str, Any]] = []
    for identity, device in sorted(snapshot.site.devices.items()):
        if device_id is not None and str(identity) != device_id:
            continue
        model = snapshot.device_models[device.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        resolved, options = resolve_effective_device(device, model, table.protocol.name)
        result.append(
            {
                "device_id": str(identity),
                "enabled": device.enabled,
                "model_id": str(device.device_model_id),
                "point_table_id": str(table.point_table_id),
                "protocol": table.protocol.name,
                "host": resolved.endpoint.host,
                "port": resolved.endpoint.port,
                "protocol_options": {
                    key: "***" if any(
                        marker in key.lower()
                        for marker in ("password", "secret", "token", "credential", "key")
                    ) else value
                    for key, value in options.items()
                },
            }
        )
    if device_id is not None and not result:
        raise ValueError(f"unknown device: {device_id}")
    return result


def points(
    snapshot: ConfigSnapshot,
    device_id: str,
    point_id: str | None = None,
) -> list[dict[str, Any]]:
    """列出设备实际点表，保留完整协议地址供现场诊断。"""
    device = next(
        (device for key, device in snapshot.site.devices.items() if str(key) == device_id),
        None,
    )
    if device is None:
        raise ValueError(f"unknown device: {device_id}")
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    result: list[dict[str, Any]] = []
    for key, point in sorted(table.points.items()):
        if point_id is not None and point_id != key:
            continue
        result.append(
            {
                "device_id": device_id,
                "point_id": key,
                "protocol": table.protocol.name,
                "address": dict(point.ext),
                "access": point.access.value,
                "scale": point.scale,
                "offset": point.offset,
                "point_groups": list(point.point_groups),
            }
        )
    if point_id is not None and not result:
        raise ValueError(f"unknown point: {device_id}.{point_id}")
    return result


def sinks(snapshot: ConfigSnapshot, sink_id: str | None = None) -> list[dict[str, Any]]:
    """返回不暴露连接凭据的 Sink 概要。"""
    result: list[dict[str, Any]] = []
    for identity, sink in sorted(snapshot.sinks.items()):
        if sink_id is not None and identity != sink_id:
            continue
        result.append(
            {
                "sink_id": identity,
                "kind": sink.kind,
                "enabled": sink.enabled,
                "connection": {
                    key: value
                    for key, value in sink.connection.items()
                    if key in {
                        "host", "port", "database", "stable", "endpoint",
                        "bootstrap_servers", "topic", "tls", "client_id",
                    }
                },
            }
        )
    if sink_id is not None and not result:
        raise ValueError(f"unknown sink: {sink_id}")
    return result
