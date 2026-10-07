"""Shared Domain 模型。"""

from .device import Device, DeviceGroup, DeviceModel, DeviceType
from .identities import (
    BusinessPointId,
    DeviceGroupId,
    DeviceId,
    DeviceModelId,
    DeviceTypeId,
    PointTableId,
)
from .point import BusinessPoint, PointTable, ProtocolPoint
from .unit import Quantity, Unit, UnitCode, UNIT_CATALOG, convert_value
from .value_objects import PointAccess, Protocol, RawDataType, ValueType

__all__ = [
    "BusinessPoint",
    "BusinessPointId",
    "Device",
    "DeviceGroup",
    "DeviceGroupId",
    "DeviceId",
    "DeviceModel",
    "DeviceModelId",
    "DeviceType",
    "DeviceTypeId",
    "PointAccess",
    "PointTable",
    "PointTableId",
    "Protocol",
    "ProtocolPoint",
    "Quantity",
    "RawDataType",
    "UNIT_CATALOG",
    "Unit",
    "UnitCode",
    "ValueType",
    "convert_value",
]
