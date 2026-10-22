"""由 Gateway 已校验配置派生 Seahorse 遥测发布映射。

不猜测 DC-EMS 地址、字节序或未确认的寄存器类型；命令仅留给 Gateway。
"""
from __future__ import annotations

from pathlib import Path

from industrial_gateway.infrastructure.config.loader import GatewayConfig, load_config
from seahorse.infrastructure.modbus_publisher import PublishConfig, PublishPoint


def build_publish_config(
    gateway: GatewayConfig,
    *,
    device_ids: frozenset[str],
    available_points: dict[str, frozenset[str]],
) -> PublishConfig:
    """只有可输出且可由 Modbus 数据写入的反馈点才进入发布表。"""
    result: list[PublishPoint] = []
    for device_id in sorted(device_ids):
        device = gateway.devices.get(device_id)
        if device is None:
            raise ValueError(f"missing Gateway device: {device_id}")
        endpoint = gateway.endpoints[device.endpoint_id]
        points = gateway.point_tables[device.point_table_id].points
        defined = available_points.get(device_id, frozenset())
        for point_id, point in sorted(points.items()):
            if point.role != "feedback":
                continue
            if point_id not in defined:
                raise ValueError(f"Seahorse has no output: {device_id}.{point_id}")
            if point.address.register_type not in {"holding", "coil"}:
                raise ValueError(f"Gateway feedback is not writable: {device_id}.{point_id}")
            if point.data_type not in {"bool", "float32", "int32"}:
                raise ValueError(f"unsupported feedback encoding: {device_id}.{point_id}")
            result.append(
                PublishPoint(
                    device_id=device_id,
                    point_id=point_id,
                    host=endpoint.host,
                    port=endpoint.port,
                    unit_id=device.protocol_address.unit_id,
                    address=point.address.address,
                    area=point.address.register_type,
                    data_type=point.data_type,
                    word_order=point.address.word_order,
                )
            )
    if not result:
        raise ValueError("Gateway profile has no publishable feedback")
    return PublishConfig(points=result)


def build_publish_config_from_file(
    path: Path,
    *,
    device_ids: frozenset[str],
    available_points: dict[str, frozenset[str]],
) -> PublishConfig:
    return build_publish_config(
        load_config(path), device_ids=device_ids, available_points=available_points
    )
