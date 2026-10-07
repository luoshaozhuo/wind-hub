"""Shared Core 应用配置身份类型。"""

from typing import NewType

ConnectionId = NewType("ConnectionId", str)

__all__ = ["ConnectionId"]
