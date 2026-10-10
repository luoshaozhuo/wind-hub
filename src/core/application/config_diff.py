"""完整配置快照差异：纯计算，不依赖文件系统或运行时。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Generic, TypeVar

from .config_snapshot import ConfigSnapshot

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class MappingDiff(Generic[_T]):
    added: frozenset[str]
    removed: frozenset[str]
    changed: frozenset[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


@dataclass(frozen=True, slots=True)
class ConfigSnapshotDiff:
    sections: Mapping[str, MappingDiff[object]]
    system_changed: bool
    site_changed: bool

    @property
    def has_changes(self) -> bool:
        return self.system_changed or self.site_changed or any(
            section.has_changes for section in self.sections.values()
        )


def _diff_mapping(old: Mapping[object, object], new: Mapping[object, object]) -> MappingDiff[object]:
    old_keys = set(old)
    new_keys = set(new)
    common = old_keys & new_keys
    return MappingDiff(
        added=frozenset(str(key) for key in new_keys - old_keys),
        removed=frozenset(str(key) for key in old_keys - new_keys),
        changed=frozenset(str(key) for key in common if old[key] != new[key]),
    )


def diff_config_snapshots(old: ConfigSnapshot, new: ConfigSnapshot) -> ConfigSnapshotDiff:
    """比较快照全部配置节；设备逐个比较，避免重复保存 Site.devices。"""
    names = (
        "device_types", "device_models", "device_groups", "point_tables",
        "business_points", "tasks", "sinks", "protocol_options_by_device",
    )
    sections = {
        name: _diff_mapping(getattr(old, name), getattr(new, name))
        for name in names
    }
    sections["devices"] = _diff_mapping(old.site.devices, new.site.devices)
    return ConfigSnapshotDiff(
        sections=sections,
        system_changed=old.system != new.system,
        site_changed=(
            old.site.site_id != new.site.site_id
            or old.site.name != new.site.name
        ),
    )
