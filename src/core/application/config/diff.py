"""Shared Core 配置差异。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeVar

from .snapshot import CoreConfigSnapshot

_KeyT = TypeVar("_KeyT")
_ValueT = TypeVar("_ValueT")


@dataclass(frozen=True, slots=True)
class IndexDiff:
    """一个稳定身份索引的结构差异。"""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoreConfigDiff:
    """两个 Shared Core 配置快照之间的结构化差异。"""

    device_types: IndexDiff
    device_models: IndexDiff
    device_groups: IndexDiff
    devices: IndexDiff
    business_points: IndexDiff
    point_tables: IndexDiff
    device_connections: IndexDiff

    @property
    def changed(self) -> bool:
        """是否存在任何配置变化。"""
        return any(
            diff.added or diff.removed or diff.updated
            for diff in (
                self.device_types,
                self.device_models,
                self.device_groups,
                self.devices,
                self.business_points,
                self.point_tables,
                self.device_connections,
            )
        )


def compute_core_config_diff(
    old: CoreConfigSnapshot,
    new: CoreConfigSnapshot,
) -> CoreConfigDiff:
    """计算两个 Shared Core 配置快照的结构化差异。"""
    return CoreConfigDiff(
        device_types=_diff_index(old.device_types, new.device_types),
        device_models=_diff_index(old.device_models, new.device_models),
        device_groups=_diff_index(old.device_groups, new.device_groups),
        devices=_diff_index(old.devices, new.devices),
        business_points=_diff_index(old.business_points, new.business_points),
        point_tables=_diff_index(old.point_tables, new.point_tables),
        device_connections=_diff_index(
            old.device_connections,
            new.device_connections,
        ),
    )


def _diff_index(
    old: Mapping[_KeyT, _ValueT],
    new: Mapping[_KeyT, _ValueT],
) -> IndexDiff:
    old_ids = set(old)
    new_ids = set(new)
    shared = old_ids & new_ids
    updated_ids = {key for key in shared if old[key] != new[key]}

    return IndexDiff(
        added=tuple(sorted((str(key) for key in new_ids - old_ids))),
        removed=tuple(sorted((str(key) for key in old_ids - new_ids))),
        updated=tuple(sorted((str(key) for key in updated_ids))),
        unchanged=tuple(sorted((str(key) for key in shared - updated_ids))),
    )
