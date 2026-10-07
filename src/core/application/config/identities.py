"""Shared Core 应用配置身份类型。"""

from typing import NewType

ConnectionId = NewType("ConnectionId", str)
PointTableId = NewType("PointTableId", str)

__all__ = ["ConnectionId", "PointTableId"]
