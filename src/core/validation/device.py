"""设备相关跨聚合引用一致性校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.config import DeviceConnection
from core.domain import (
    ConnectionId,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    PointTable,
    PointTableId,
)


def validate_device_models(
    device_models: Mapping[DeviceModelId, DeviceModel],
    point_tables: Mapping[PointTableId, PointTable],
) -> None:
    """校验 DeviceModel 对 PointTable 的跨聚合引用完整性。"""
    for model in device_models.values():
        if model.point_table_id not in point_tables:
            raise ValueError(
                f"device model '{model.device_model_id}' references unknown point table "
                f"'{model.point_table_id}'"
            )


def validate_device_references(
    devices: Sequence[Device],
    device_models: Mapping[DeviceModelId, DeviceModel],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
) -> None:
    """校验 Device 对 DeviceModel 与 DeviceGroup 的跨聚合引用完整性。"""
    for device in devices:
        if device.device_model_id not in device_models:
            raise ValueError(
                f"device '{device.device_id}' references unknown device model "
                f"'{device.device_model_id}'"
            )
        for group_id in device.device_group_ids:
            if group_id not in device_groups:
                raise ValueError(
                    f"device '{device.device_id}' references unknown device group "
                    f"'{group_id}'"
                )


def validate_device_connections(
    connections: Sequence[DeviceConnection],
    devices: Mapping[DeviceId, Device],
) -> None:
    """校验设备通信接入配置引用完整性。

    规则：
    1. connection_id 在连接集合中唯一；
    2. DeviceConnection.device_id 必须引用已存在 Device。

    PointTable 不再由 DeviceConnection 直接引用，而通过
    Device -> DeviceModel -> PointTable 唯一解析；对应引用完整性分别由
    validate_device_references() 与 validate_device_models() 校验。

    同一设备允许存在多个连接，且 endpoint 不要求唯一。Worker / Session
    的一对一运行约束属于 Runtime。
    """
    seen_connection_ids: set[ConnectionId] = set()

    for connection in connections:
        if connection.connection_id in seen_connection_ids:
            raise ValueError(
                f"duplicate device connection id '{connection.connection_id}'"
            )
        seen_connection_ids.add(connection.connection_id)

        if connection.device_id not in devices:
            raise ValueError(
                f"connection '{connection.connection_id}' references unknown device "
                f"'{connection.device_id}'"
            )
