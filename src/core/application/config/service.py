"""Shared Core 配置应用服务。"""

from __future__ import annotations

from dataclasses import dataclass

from .codec import CoreConfigArtifact, CoreConfigCodecPort
from .diff import CoreConfigDiff, compute_core_config_diff
from .repository import (
    ConfigRevision,
    ConfigRevisionConflict,
    CoreConfigRepositoryPort,
    StoredCoreConfig,
)
from .snapshot import CoreConfigSnapshot
from .validation import validate_core_config
from .validator import CoreConfigValidatorPort


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

    def __init__(
        self,
        repository: CoreConfigRepositoryPort,
        codec: CoreConfigCodecPort | None = None,
        validators: tuple[CoreConfigValidatorPort, ...] = (),
    ) -> None:
        self._repository = repository
        self._codec = codec
        self._validators = validators

    async def get(self) -> StoredCoreConfig:
        """读取当前共享配置。"""
        return await self._repository.load()

    async def export(self) -> CoreConfigArtifact:
        """导出当前 Shared Core 配置。"""
        codec = self._require_codec()
        current = await self._repository.load()
        return codec.encode(current.snapshot)

    async def import_replace(
        self,
        artifact: CoreConfigArtifact,
        *,
        expected_revision: ConfigRevision,
    ) -> CoreConfigUpdateResult:
        """解析外部配置制品并原子替换当前配置。"""
        codec = self._require_codec()
        snapshot = codec.decode(artifact)
        return await self.replace(snapshot, expected_revision=expected_revision)

    async def replace(
        self,
        snapshot: CoreConfigSnapshot,
        *,
        expected_revision: ConfigRevision,
    ) -> CoreConfigUpdateResult:
        """以新快照替换当前共享配置，并返回结构化差异。"""
        validate_core_config(snapshot)
        for validator in self._validators:
            validator.validate(snapshot)

        current = await self._repository.load()
        if current.revision != expected_revision:
            raise ConfigRevisionConflict(
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


    def _require_codec(self) -> CoreConfigCodecPort:
        """返回已注入 Codec；未配置时明确失败。"""
        if self._codec is None:
            raise RuntimeError("config codec is not wired")
        return self._codec
