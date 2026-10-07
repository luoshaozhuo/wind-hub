"""Shared Core 配置仓储端口与版本契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType, Protocol

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


class CoreConfigRepositoryPort(Protocol):
    """Shared Core 配置持久化能力边界。

    Repository 负责原子读取/写入与乐观并发控制；YAML、数据库、Git 等具体
    存储实现属于 Infrastructure。
    """

    async def load(self) -> StoredCoreConfig:
        """读取当前配置及其修订号。"""
        ...

    async def save(
        self,
        snapshot: CoreConfigSnapshot,
        *,
        expected_revision: ConfigRevision,
    ) -> StoredCoreConfig:
        """原子保存配置。

        当 expected_revision 与当前持久化版本不一致时，实现必须拒绝覆盖并抛出
        ConfigRevisionConflict，而不是静默覆盖他人修改。
        """
        ...
