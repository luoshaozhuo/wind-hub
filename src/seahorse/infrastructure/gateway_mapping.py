"""校验 Seahorse 发布点与 Gateway 真实配置的逐点契约。

只读解析，不依赖 Gateway 内部模块，也不修改生产配置。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from seahorse.infrastructure.modbus_publisher import PublishConfig


def validate_gateway_mapping(config: PublishConfig, gateway_path: Path) -> None:
    """启动前拒绝误发命令点、错地址和错误编码。

    前提是 Gateway 和 Seahorse 在同一配置版本下运行。
    """
    raw: Any = yaml.safe_load(gateway_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("invalid gateway configuration")
    endpoints = raw.get("endpoints")
    devices = raw.get("devices")
    point_tables = raw.get("point_tables")
    if not all(isinstance(x, dict) for x in (endpoints, devices, point_tables)):
        raise ValueError("missing gateway endpoints/devices/point_tables")
    for point in config.points:
        device: Any = devices.get(point.device_id)
        if not isinstance(device, dict):
            raise ValueError(f"gateway device missing: {point.device_id}")
        endpoint: Any = endpoints.get(device.get("endpoint_id"))
        table: Any = point_tables.get(device.get("point_table_id"))
        if not isinstance(endpoint, dict) or not isinstance(table, dict):
            raise ValueError(f"gateway reference missing: {point.device_id}")
        address: Any = device.get("protocol_address", {})
        if (
            endpoint.get("host") != point.host
            or endpoint.get("port") != point.port
            or address.get("unit_id") != point.unit_id
        ):
            raise ValueError(f"gateway endpoint mismatch: {point.device_id}")
        definitions: Any = table.get("points", {})
        definition: Any = definitions.get(point.point_id)
        if not isinstance(definition, dict) or definition.get("role") != "feedback":
            raise ValueError(f"not gateway feedback: {point.device_id}.{point.point_id}")
        location: Any = definition.get("address", {})
        if (
            definition.get("data_type") != point.data_type
            or location.get("register_type") != point.area
            or location.get("address") != point.address
            or location.get("word_order", "big_endian") != point.word_order
        ):
            raise ValueError(f"gateway point mismatch: {point.device_id}.{point.point_id}")
