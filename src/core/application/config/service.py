"""Shared Core 配置应用服务。"""

from __future__ import annotations

from dataclasses import dataclass

from .diff import CoreConfigDiff, compute_core_config_diff
from .repository import ConfigRevision, CoreConfigRepositoryPort, StoredCoreConfig
from .snapshot import CoreConfigSnapshot
from .validation import validate_core_config


@dataclass(frozen=True, slots=True)
class CoreConfigUpdateResult:
    """一次配置替换的结果。"""

    stored: StoredCoreConfig
    diff: CoreConfigDiff


class CoreConfigService:
    """共享配置读取与原子替换用例。

    本服务只负责应用级流程：校验 -> 计算差异 -> 乐观并发保存。
    文件格式、目录结构、数据库事务或 Git commit 等细节由 Repository Adapter 负责。
    """

    def __init__(self, repository: CoreConfigRepositoryPort) -> None:
        self._repository = repository

    async def get(self) -> StoredCoreConfig:
        """读取当前共享配置。"""
        return await self._repository.load()

    async def replace(
        self,
        snapshot: CoreConfigSnapshot,
        *,
        expected_revision: ConfigRevision,
    ) -> CoreConfigUpdateResult:
        """以新快照替换当前共享配置，并返回结构化差异。"""
        validate_core_config(snapshot)

        current = await self._repository.load()
        if current.revision != expected_revision:
            raise ValueError(
                f"config revision conflict: expected '{expected_revision}', "
                f"current '{current.revision}'"
            )

        diff = compute_core_config_diff(current.snapshot, snapshot)
        if not diff.changed:
            return CoreConfigUpdateResult(stored=current, diff=diff)

        stored = await self._repository.save(
            snapshot,
            expected_revision=expected_revision,
        )
        return CoreConfigUpdateResult(stored=stored, diff=diff)
