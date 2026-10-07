"""Shared Domain 全局身份类型。

使用 NewType 区分不同聚合的稳定身份。运行时仍保持 str 语义，
仅用于静态类型检查，避免不同 ID 在跨聚合引用中被误传。
"""

from typing import NewType

BusinessPointId = NewType("BusinessPointId", str)
ConnectionId = NewType("ConnectionId", str)
DeviceGroupId = NewType("DeviceGroupId", str)
DeviceId = NewType("DeviceId", str)
DeviceModelId = NewType("DeviceModelId", str)
DeviceTypeId = NewType("DeviceTypeId", str)
PointTableId = NewType("PointTableId", str)

__all__ = [
    "BusinessPointId",
    "ConnectionId",
    "DeviceGroupId",
    "DeviceId",
    "DeviceModelId",
    "DeviceTypeId",
    "PointTableId",
]
