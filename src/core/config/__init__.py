"""新 Core 的共享静态配置语义。"""

from .device_connection import ConnectionEndpoint, DeviceConnection
from .snapshot import ConfigSnapshot

__all__ = [
    "ConfigSnapshot",
    "ConnectionEndpoint",
    "DeviceConnection",
]
