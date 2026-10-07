"""Shared Core 配置 ports。

CoreConfigPort 是上层调用的配置用例入口；Repository/Codec/Validator Port 是
Application 对 Infrastructure 的依赖边界。本模块只定义接口。
"""

from __future__ import annotations

from typing import Protocol

from core.domain import CoreConfigSnapshot

from ..config_contract import (
    ConfigRevision,
    CoreConfigArtifact,
    CoreConfigPreview,
    CoreConfigUpdateResult,
    StoredCoreConfig,
)


class CoreConfigPort(Protocol):
    """Shared Core 对上层暴露的完整配置用例能力。"""

    async def get(self) -> StoredCoreConfig:
        ...

    def validate(self, snapshot: CoreConfigSnapshot) -> None:
        ...

    async def preview_replace(
        self,
        snapshot: CoreConfigSnapshot,
    ) -> CoreConfigPreview:
        ...

    async def import_preview(
        self,
        artifact: CoreConfigArtifact,
    ) -> CoreConfigPreview:
        ...

    async def export(self) -> CoreConfigArtifact:
        ...

    async def import_replace(
        self,
        artifact: CoreConfigArtifact,
        *,
        expected_revision: ConfigRevision,
    ) -> CoreConfigUpdateResult:
        ...

    async def replace(
        self,
        snapshot: CoreConfigSnapshot,
        *,
        expected_revision: ConfigRevision,
    ) -> CoreConfigUpdateResult:
        ...


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
