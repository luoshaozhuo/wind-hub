"""新 Core 的共享静态配置语义。"""

from .device_connection import ConnectionEndpoint, DeviceConnection
from .identities import PointSetId
from .point_set import PointSet
from .snapshot import ConfigSnapshot

__all__ = [
    "ConfigSnapshot",
    "ConnectionEndpoint",
    "DeviceConnection",
    "PointSet",
    "PointSetId",
]
