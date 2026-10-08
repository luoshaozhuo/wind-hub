"""领域配置索引上的无状态查找。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..device import Device, DeviceModel
from ..identities import DeviceId, DeviceModelId, PointTableId
from ..point import PointTable
from .options import ProtocolOptions


def point_table_for_device(
    devices: Mapping[DeviceId, Device],
    device_models: Mapping[DeviceModelId, DeviceModel],
    point_tables: Mapping[PointTableId, PointTable],
    device_id: DeviceId,
) -> PointTable:
    """解析 Device -> DeviceModel -> PointTable。"""
    device = devices[device_id]
    model = device_models[device.device_model_id]
    return point_tables[model.point_table_id]


def device_options_for(
    device_options: Mapping[DeviceId, ProtocolOptions],
    device_id: DeviceId,
) -> ProtocolOptions:
    """返回指定 Device 的协议专有连接配置；缺省为空映射。"""
    return device_options.get(device_id, MappingProxyType({}))


__all__ = ["device_options_for", "point_table_for_device"]
