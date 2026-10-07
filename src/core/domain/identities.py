"""Shared Domain 全局身份类型。"""

from typing import NewType

BusinessPointId = NewType("BusinessPointId", str)
DeviceGroupId = NewType("DeviceGroupId", str)
DeviceId = NewType("DeviceId", str)
DeviceModelId = NewType("DeviceModelId", str)
DeviceTypeId = NewType("DeviceTypeId", str)
PointTableId = NewType("PointTableId", str)

__all__ = [
    "BusinessPointId",
    "DeviceGroupId",
    "DeviceId",
    "DeviceModelId",
    "DeviceTypeId",
    "PointTableId",
]
