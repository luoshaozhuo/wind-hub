"""Shared Core 的应用级配置模型与校验。"""

from .device_connection import ConnectionEndpoint, DeviceConnection
from .snapshot import CoreConfigSnapshot
from .validation import validate_core_config

__all__ = [
    "ConnectionEndpoint",
    "CoreConfigSnapshot",
    "DeviceConnection",
    "validate_core_config",
]
