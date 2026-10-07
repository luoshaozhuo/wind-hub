"""Shared Core 配置应用服务。"""

from __future__ import annotations

from core.domain.config import (
    CoreConfigSnapshot,
    compute_core_config_diff,
    validate_core_config,
)

from .config_contract import (
    ConfigRevision,
    ConfigRevisionConflict,
    CoreConfigArtifact,
    CoreConfigPreview,
    CoreConfigUpdateResult,
    StoredCoreConfig,
)
from .errors import ConfigError
from .port import (
    CoreConfigCodecPort,
    CoreConfigRepositoryPort,
    CoreConfigValidatorPort,
)


class CoreConfigService:
    """共享配置用例编排。

    负责校验、预览、导入导出、版本检查与保存流程；不承担领域规则、
    文件格式或持久化实现。
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

    def validate(self, snapshot: CoreConfigSnapshot) -> None:
        """执行领域校验与已注入的适配器静态校验。"""
        try:
            validate_core_config(snapshot)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        for validator in self._validators:
            validator.validate(snapshot)

    async def preview_replace(
        self,
        snapshot: CoreConfigSnapshot,
    ) -> CoreConfigPreview:
        """校验候选配置并预览结构化差异。"""
        self.validate(snapshot)
        current = await self._repository.load()
        return CoreConfigPreview(
            current_revision=current.revision,
            diff=compute_core_config_diff(current.snapshot, snapshot),
        )

    async def import_preview(
        self,
        artifact: CoreConfigArtifact,
    ) -> CoreConfigPreview:
        """解析外部制品并预览，不写入。"""
        codec = self._require_codec()
        return await self.preview_replace(codec.decode(artifact))

    async def export(self) -> CoreConfigArtifact:
        """导出当前配置。"""
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
        return await self.replace(
            snapshot,
            expected_revision=expected_revision,
        )

    async def replace(
        self,
        snapshot: CoreConfigSnapshot,
        *,
        expected_revision: ConfigRevision,
    ) -> CoreConfigUpdateResult:
        """以新快照替换当前配置，并返回差异。"""
        self.validate(snapshot)

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
        if self._codec is None:
            raise RuntimeError("config codec is not wired")
        return self._codec
