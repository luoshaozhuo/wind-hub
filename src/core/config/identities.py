"""共享配置身份类型。"""

from typing import NewType

PointSetId = NewType("PointSetId", str)
SinkId = NewType("SinkId", str)
TaskId = NewType("TaskId", str)

__all__ = [
    "PointSetId",
    "SinkId",
    "TaskId",
]
