"""Shared Core 配置 outbound ports。

本模块只定义 Application 依赖的接口，不放 DTO、值对象、实现或业务逻辑。
"""

from __future__ import annotations

from typing import Protocol

from ..config.artifact import CoreConfigArtifact
from ..config.revision import ConfigRevision, StoredCoreConfig
from ..config.snapshot import CoreConfigSnapshot


class CoreConfigCodecPort(Protocol):
    """配置快照与外部制品之间的编解码边界。"""

    def encode(self, snapshot: CoreConfigSnapshot) -> CoreConfigArtifact:
        ...

    def decode(self, artifact: CoreConfigArtifact) -> CoreConfigSnapshot:
        ...


class CoreConfigRepositoryPort(Protocol):
    """Shared Core 配置持久化边界。"""

    async def load(self) -> StoredCoreConfig:
        ...

    async def save(
        self,
        snapshot: CoreConfigSnapshot,
        *,
        expected_revision: ConfigRevision,
    ) -> StoredCoreConfig:
        ...


class CoreConfigValidatorPort(Protocol):
    """配置激活前的附加静态校验边界。"""

    def validate(self, snapshot: CoreConfigSnapshot) -> None:
        ...
