"""Shared Core 配置持久化版本值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

from ..errors import ConfigError
from .snapshot import CoreConfigSnapshot

ConfigRevision = NewType("ConfigRevision", str)


class ConfigRevisionConflict(ConfigError):
    """配置乐观并发冲突。"""


@dataclass(frozen=True, slots=True)
class StoredCoreConfig:
    """带持久化修订号的 Shared Core 配置快照。"""

    snapshot: CoreConfigSnapshot
    revision: ConfigRevision

    def __post_init__(self) -> None:
        revision = self.revision.strip()
        if not revision:
            raise ConfigError("config revision must not be empty")
        object.__setattr__(self, "revision", ConfigRevision(revision))
