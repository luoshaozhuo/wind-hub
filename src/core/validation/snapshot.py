"""完整共享配置快照的一致性校验。"""

from __future__ import annotations

from core.config import ConfigSnapshot

from .device import (
    validate_device_connections,
    validate_device_models,
    validate_device_references,
)
from .point import validate_point_sets, validate_point_tables


def validate_config_snapshot(snapshot: ConfigSnapshot) -> None:
    """校验 ConfigSnapshot 中全部已知跨对象引用关系。

    ConfigSnapshot 自身只保证不可变索引及索引键与对象身份一致；
    本函数负责跨 Domain Aggregate 与 Config Object 的引用完整性。
    """
    validate_device_models(
        snapshot.device_models,
        snapshot.device_types,
        snapshot.point_tables,
    )
    validate_device_references(
        tuple(snapshot.devices.values()),
        snapshot.device_models,
        snapshot.device_groups,
    )
    validate_point_tables(
        tuple(snapshot.point_tables.values()),
        snapshot.business_points,
    )
    validate_point_sets(
        tuple(snapshot.point_sets.values()),
        snapshot.business_points,
    )
    validate_device_connections(
        tuple(snapshot.device_connections.values()),
        snapshot.devices,
    )
