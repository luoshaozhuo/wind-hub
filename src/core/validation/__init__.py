"""新 Core 跨对象一致性校验。"""

from .device import (
    validate_device_connections,
    validate_device_models,
    validate_device_references,
)
from .point import (
    validate_business_points,
    validate_point_sets,
    validate_point_tables,
)
from .snapshot import validate_config_snapshot
from .task import validate_collection_tasks

__all__ = [
    "validate_business_points",
    "validate_collection_tasks",
    "validate_config_snapshot",
    "validate_device_connections",
    "validate_device_models",
    "validate_device_references",
    "validate_point_sets",
    "validate_point_tables",
]
