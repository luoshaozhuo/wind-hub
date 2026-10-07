"""共享配置身份类型。"""

from typing import NewType

PointSetId = NewType("PointSetId", str)

__all__ = [
    "PointSetId",
]
