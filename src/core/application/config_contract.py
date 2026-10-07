"""Shared Core 配置应用契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

from core.domain.config import CoreConfigDiff, CoreConfigSnapshot

from .errors import ConfigError

ConfigRevision = NewType("ConfigRevision", str)


class ConfigRevisionConflict(ConfigError):
    """配置乐观并发冲突。"""


@dataclass(frozen=True, slots=True)
class CoreConfigArtifact:
    """一个可导入/导出的配置制品。"""

    content: bytes
    media_type: str

    def __post_init__(self) -> None:
        media_type = self.media_type.strip().lower()
        if not media_type:
            raise ConfigError("config artifact media_type must not be empty")
        object.__setattr__(self, "media_type", media_type)


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


@dataclass(frozen=True, slots=True)
class CoreConfigPreview:
    """一次配置替换预览。"""

    current_revision: ConfigRevision
    diff: CoreConfigDiff


@dataclass(frozen=True, slots=True)
class CoreConfigUpdateResult:
    """一次配置替换结果。"""

    stored: StoredCoreConfig
    diff: CoreConfigDiff
