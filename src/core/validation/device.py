"""设备相关跨聚合引用一致性校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.config import DeviceConnection
from core.domain import Device, DeviceGroup, DeviceModel, PointTable


def validate_device_references(
    devices: Sequence[Device],
    device_models: Mapping[str, DeviceModel],
    device_groups: Mapping[str, DeviceGroup],
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
    devices: Mapping[str, Device],
    device_models: Mapping[str, DeviceModel],
    point_tables: Mapping[str, PointTable],
) -> None:
    """校验设备通信接入配置与 Shared Domain 的一致性。

    规则：
    1. connection_id 在连接集合中唯一；
    2. 同一个 ``(device_id, point_table_id)`` 只允许一个 DeviceConnection；
    3. DeviceConnection.device_id 必须引用已存在 Device；
    4. Device.device_model_id 必须引用已存在 DeviceModel；
    5. DeviceConnection.point_table_id 必须存在，并属于该设备型号声明支持的点表。

    协议无需单独比较，因为 DeviceConnection 不保存 protocol；最终协议唯一来自
    PointTable.protocol。Worker / Session 的一对一运行约束属于 Runtime，不在此
    创建或管理运行对象。
    """
    seen_connection_ids: set[str] = set()
    seen_bindings: set[tuple[str, str]] = set()

    for connection in connections:
        if connection.connection_id in seen_connection_ids:
            raise ValueError(
                f"duplicate device connection id '{connection.connection_id}'"
            )
        seen_connection_ids.add(connection.connection_id)

        binding = (connection.device_id, connection.point_table_id)
        if binding in seen_bindings:
            raise ValueError(
                "duplicate device connection for "
                f"device '{connection.device_id}' and point table "
                f"'{connection.point_table_id}'"
            )
        seen_bindings.add(binding)

        device = devices.get(connection.device_id)
        if device is None:
            raise ValueError(
                f"connection '{connection.connection_id}' references unknown device "
                f"'{connection.device_id}'"
            )

        model = device_models.get(device.device_model_id)
        if model is None:
            raise ValueError(
                f"device '{device.device_id}' references unknown device model "
                f"'{device.device_model_id}'"
            )

        if connection.point_table_id not in point_tables:
            raise ValueError(
                f"connection '{connection.connection_id}' references unknown point table "
                f"'{connection.point_table_id}'"
            )

        if not model.supports_point_table(connection.point_table_id):
            raise ValueError(
                f"connection '{connection.connection_id}' uses point table "
                f"'{connection.point_table_id}', but device model "
                f"'{model.device_model_id}' does not support it"
            )
