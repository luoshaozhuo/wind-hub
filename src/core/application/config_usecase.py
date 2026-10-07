"""Shared Core 配置应用用例。"""

from __future__ import annotations

from core.domain import CoreConfigSnapshot, validate_core_config

from .port import ConfigRepositoryPort


class LoadConfig:
    """加载共享配置并执行领域一致性校验。"""

    def __init__(self, repository: ConfigRepositoryPort) -> None:
        self._repository = repository

    async def execute(self) -> CoreConfigSnapshot:
        snapshot = await self._repository.load()
        validate_core_config(snapshot)
        return snapshot


class SaveConfig:
    """校验共享配置并保存。"""

    def __init__(self, repository: ConfigRepositoryPort) -> None:
        self._repository = repository

    async def execute(self, snapshot: CoreConfigSnapshot) -> None:
        validate_core_config(snapshot)
        await self._repository.save(snapshot)
