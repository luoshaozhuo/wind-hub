"""新 Core 的共享静态配置语义。"""

from .device_connection import ConnectionEndpoint, DeviceConnection
from .identities import PointSetId, SinkId, TaskId
from .point_set import PointSet
from .snapshot import ConfigSnapshot
from .task import CollectionTask

__all__ = [
    "CollectionTask",
    "ConfigSnapshot",
    "ConnectionEndpoint",
    "DeviceConnection",
    "PointSet",
    "PointSetId",
    "SinkId",
    "TaskId",
]
