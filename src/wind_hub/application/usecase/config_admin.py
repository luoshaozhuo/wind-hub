"""配置文件管理、校验、Apply、备份与 revision 历史。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from wind_hub.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config


CONFIG_FILES = (
    "system.yaml",
    "units.yaml",
    "device_models.yaml",
    "points.yaml",
    "devices.yaml",
    "tasks.yaml",
    "reporting.yaml",
)


class ConfigFileInfo(BaseModel):
    """配置文件列表项。"""

    name: str
    exists: bool
    optional: bool = False


class ConfigReview(BaseModel):
    """候选配置校验与 diff 结果。"""

    name: str
    valid: bool
    changed: bool = False
    errors: list[str] = Field(default_factory=list)
    diff: dict[str, object] = Field(default_factory=dict)


class ConfigApplyResult(BaseModel):
    """Apply/Restore 结果。"""

    success: bool
    revision: int | None = None
    errors: list[str] = Field(default_factory=list)
    rollback_performed: bool = False


class ConfigRevisionInfo(BaseModel):
    """配置 revision 元数据。"""

    revision: int
    created_at: datetime
    source: str
    comment: str = ""


class ConfigAdminUseCase:
    """以现有 YAML 配置目录为唯一持久化源的管理用例。"""

    def __init__(self, config: ConfigUseCase) -> None:
        self._config = config
        self._base = config.config_dir
        self._history = self._base / ".history"
        self._apply_lock = asyncio.Lock()

    def list_files(self) -> list[ConfigFileInfo]:
        """列出固定配置集；reporting.yaml 是唯一可选文件。"""
        return [
            ConfigFileInfo(
                name=name,
                exists=(self._base / name).is_file(),
                optional=name == "reporting.yaml",
            )
            for name in CONFIG_FILES
        ]

    def read_file(self, name: str) -> str:
        """读取单个配置文件；未知/不存在文件抛 KeyError。"""
        path = self._path(name)
        if not path.is_file():
            raise KeyError(name)
        return path.read_text(encoding="utf-8")

    def validate_file(self, name: str, content: str) -> ConfigReview:
        """把候选文本放入完整临时配置集后执行正式 load_config 校验。"""
        try:
            candidate = self._load_candidate(name, content)
        except Exception as exc:
            return ConfigReview(name=name, valid=False, errors=[str(exc)])
        diff = compute_diff(self._config.current_config, candidate)
        return ConfigReview(
            name=name,
            valid=True,
            changed=diff.has_any_changes,
            diff=diff.model_dump(mode="json"),
        )

    async def apply_file(
        self, name: str, content: str, *, source: str = "api", comment: str = ""
    ) -> ConfigApplyResult:
        """串行化配置 Apply，避免并发替换同一配置集。"""
        async with self._apply_lock:
            return await self._apply_file_locked(
                name, content, source=source, comment=comment
            )

    async def _apply_file_locked(
        self, name: str, content: str, *, source: str, comment: str
    ) -> ConfigApplyResult:
        """校验并原子替换单文件；reload 失败时恢复文件并重新加载旧配置。"""
        review = self.validate_file(name, content)
        if not review.valid:
            return ConfigApplyResult(success=False, errors=review.errors)
        path = self._path(name)
        old_exists = path.is_file()
        old_content = path.read_text(encoding="utf-8") if old_exists else None
        if old_content == content:
            return ConfigApplyResult(success=True)
        self._atomic_write(path, content)
        result = await self._config.reload()
        if not result.success:
            self._restore_file(path, old_exists, old_content)
            rollback = await self._config.reload()
            errors = list(result.errors)
            if not rollback.success:
                errors.append(f"rollback reload failed: {rollback.errors}")
            return ConfigApplyResult(
                success=False, errors=errors, rollback_performed=True
            )
        revision = self._record_revision(source=source, comment=comment)
        return ConfigApplyResult(success=True, revision=revision)

    async def apply_files(
        self,
        files: dict[str, str],
        *,
        source: str = "api",
        comment: str = "",
    ) -> ConfigApplyResult:
        """原子应用一组配置文件，并且只推进一次 revision。"""
        if not files:
            return ConfigApplyResult(success=True)
        async with self._apply_lock:
            try:
                candidate = self._load_candidates(files)
            except Exception as exc:
                return ConfigApplyResult(success=False, errors=[str(exc)])
            diff = compute_diff(self._config.current_config, candidate)
            if not diff.has_any_changes:
                return ConfigApplyResult(success=True)
            previous = {
                name: (self._base / name).read_bytes()
                for name in CONFIG_FILES
                if (self._base / name).is_file()
            }
            try:
                for name, content in files.items():
                    self._atomic_write(self._path(name), content)
                result = await self._config.reload()
            except Exception as exc:
                result = None
                errors = [str(exc) or type(exc).__name__]
            else:
                errors = list(result.errors)
            if result is None or not result.success:
                self._replace_bytes(previous)
                rollback = await self._config.reload()
                if not rollback.success:
                    errors.append(f"rollback reload failed: {rollback.errors}")
                return ConfigApplyResult(
                    success=False,
                    errors=errors,
                    rollback_performed=True,
                )
            revision = self._record_revision(source=source, comment=comment)
            return ConfigApplyResult(success=True, revision=revision)

    def backup_bytes(self) -> bytes:
        """把当前 Applied YAML 集打成 ZIP；不包含 .history。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in CONFIG_FILES:
                path = self._base / name
                if path.is_file():
                    archive.writestr(name, path.read_bytes())
        return buffer.getvalue()

    def history(self) -> list[ConfigRevisionInfo]:
        """按 revision 倒序返回历史。"""
        index = self._read_index()
        rows = [ConfigRevisionInfo.model_validate(row) for row in index]
        return sorted(rows, key=lambda row: row.revision, reverse=True)

    async def restore_revision(self, revision: int) -> ConfigApplyResult:
        """串行恢复 revision。"""
        async with self._apply_lock:
            return await self._restore_revision_locked(revision)

    async def _restore_revision_locked(self, revision: int) -> ConfigApplyResult:
        """恢复 revision 的完整配置快照；失败则回滚恢复前文件集。"""
        source_dir = self._history / f"{revision:06d}"
        if not source_dir.is_dir():
            raise KeyError(revision)
        previous = {
            name: (self._base / name).read_bytes()
            for name in CONFIG_FILES
            if (self._base / name).is_file()
        }
        self._replace_from_snapshot(source_dir)
        result = await self._config.reload()
        if not result.success:
            self._replace_bytes(previous)
            rollback = await self._config.reload()
            errors = list(result.errors)
            if not rollback.success:
                errors.append(f"rollback reload failed: {rollback.errors}")
            return ConfigApplyResult(
                success=False, errors=errors, rollback_performed=True
            )
        new_revision = self._record_revision(
            source="restore", comment=f"restored revision {revision}"
        )
        return ConfigApplyResult(success=True, revision=new_revision)

    def _load_candidate(self, name: str, content: str) -> Config:
        """构造单文件候选配置集并返回正式 Config。"""
        return self._load_candidates({name: content})

    def _load_candidates(self, files: dict[str, str]) -> Config:
        """构造多文件候选配置集并执行正式完整加载。"""
        for name in files:
            self._path(name)
        with tempfile.TemporaryDirectory(prefix="wind-hub-config-") as tmp:
            target = Path(tmp)
            for file_name in CONFIG_FILES:
                source = self._base / file_name
                if source.is_file():
                    shutil.copy2(source, target / file_name)
            for name, content in files.items():
                (target / name).write_text(content, encoding="utf-8")
            return load_config(target)

    def _path(self, name: str) -> Path:
        """约束文件名，禁止目录穿越。"""
        if name not in CONFIG_FILES:
            raise KeyError(name)
        return self._base / name

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """同目录临时文件 + os.replace 原子替换。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def _restore_file(path: Path, existed: bool, content: str | None) -> None:
        """恢复单文件旧版本。"""
        if existed and content is not None:
            ConfigAdminUseCase._atomic_write(path, content)
        elif path.exists():
            path.unlink()

    def _record_revision(self, *, source: str, comment: str) -> int:
        """保存当前完整 Applied 快照并更新 index.json。"""
        rows = self._read_index()
        revision = max((int(row["revision"]) for row in rows), default=0) + 1
        self._history.mkdir(parents=True, exist_ok=True)
        target = self._history / f"{revision:06d}"
        target.mkdir(parents=False, exist_ok=False)
        for name in CONFIG_FILES:
            path = self._base / name
            if path.is_file():
                shutil.copy2(path, target / name)
        rows.append(
            {
                "revision": revision,
                "created_at": datetime.now(UTC).isoformat(),
                "source": source,
                "comment": comment,
            }
        )
        (self._history / "index.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return revision

    def _read_index(self) -> list[dict[str, object]]:
        """读取 revision index；不存在时返回空列表。"""
        path = self._history / "index.json"
        if not path.is_file():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return cast(list[dict[str, object]], raw)

    def _replace_from_snapshot(self, source: Path) -> None:
        """用快照完整替换配置文件集合。"""
        for name in CONFIG_FILES:
            src = source / name
            dst = self._base / name
            if src.is_file():
                self._atomic_write(dst, src.read_text(encoding="utf-8"))
            elif dst.exists():
                dst.unlink()

    def _replace_bytes(self, previous: dict[str, bytes]) -> None:
        """恢复调用前的完整配置文件集合。"""
        for name in CONFIG_FILES:
            dst = self._base / name
            if name in previous:
                temp = dst.with_name(f".{dst.name}.tmp")
                temp.write_bytes(previous[name])
                os.replace(temp, dst)
            elif dst.exists():
                dst.unlink()
