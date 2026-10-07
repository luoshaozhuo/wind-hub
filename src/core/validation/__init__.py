"""新 Core 跨对象一致性校验。"""

from .device import (
    validate_device_connections,
    validate_device_models,
    validate_device_references,
)
from .point import validate_point_sets, validate_point_tables
from .snapshot import validate_config_snapshot

__all__ = [
    "validate_config_snapshot",
    "validate_device_connections",
    "validate_device_models",
    "validate_device_references",
    "validate_point_sets",
    "validate_point_tables",
]
