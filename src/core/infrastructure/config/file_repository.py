"""基于单 YAML 文件的 Shared Core 配置仓储。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from core.application.config import (
    ConfigError,
    ConfigRevision,
    ConfigRevisionConflict,
    CoreConfigArtifact,
    CoreConfigRepositoryPort,
    CoreConfigSnapshot,
    StoredCoreConfig,
    validate_core_config,
)

from .yaml_codec import YamlCoreConfigCodec


class YamlFileCoreConfigRepository(CoreConfigRepositoryPort):
    """使用单个配置文件持久化 Shared Core 配置。

    - revision 是文件内容的 SHA-256；
    - save 使用 expected_revision 做乐观并发检查；
    - 写入采用同目录临时文件 + fsync + os.replace，避免半写文件；
    - 阻塞文件 I/O 通过 asyncio.to_thread 隔离出事件循环。

    本实现假定唯一写入口通过本 Repository。跨主机共享文件系统上的分布式锁不在
    本类职责内；如需多节点并发写入，应替换为数据库/Git 等 Repository Adapter。
    """

    def __init__(
        self,
        path: str | Path,
        codec: YamlCoreConfigCodec | None = None,
    ) -> None:
        self._path = Path(path)
        self._codec = codec or YamlCoreConfigCodec()
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """返回配置文件路径。"""
        return self._path

    async def load(self) -> StoredCoreConfig:
        """读取、解析并校验当前配置。"""
        content = await asyncio.to_thread(self._read_bytes)
        artifact = _artifact_from_bytes(content)
        snapshot = self._codec.decode(artifact)
        validate_core_config(snapshot)
        return StoredCoreConfig(
            snapshot=snapshot,
            revision=_revision(content),
        )

    async def save(
        self,
        snapshot: CoreConfigSnapshot,
        *,
        expected_revision: ConfigRevision,
    ) -> StoredCoreConfig:
        """在 revision 未变化时原子替换配置文件。"""
        validate_core_config(snapshot)
        artifact = self._codec.encode(snapshot)

        async with self._write_lock:
            return await asyncio.to_thread(
                self._save_sync,
                artifact.content,
                expected_revision,
            )

    def _read_bytes(self) -> bytes:
        try:
            return self._path.read_bytes()
        except FileNotFoundError as exc:
            raise ConfigError(
                f"core config file not found: {self._path}"
            ) from exc

    def _save_sync(
        self,
        content: bytes,
        expected_revision: ConfigRevision,
    ) -> StoredCoreConfig:
        current = self._read_bytes()
        current_revision = _revision(current)
        if current_revision != expected_revision:
            raise ConfigRevisionConflict(
                f"config revision conflict: expected '{expected_revision}', "
                f"current '{current_revision}'"
            )

        snapshot = self._codec.decode(_artifact_from_bytes(content))
        validate_core_config(snapshot)

        current_mode = self._path.stat().st_mode & 0o777
        self._atomic_replace(content, mode=current_mode)
        return StoredCoreConfig(
            snapshot=snapshot,
            revision=_revision(content),
        )

    def _atomic_replace(self, content: bytes, *, mode: int) -> None:
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self._path)
            _fsync_directory(parent)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise


def _artifact_from_bytes(content: bytes) -> CoreConfigArtifact:
    return CoreConfigArtifact(
        content=content,
        media_type="application/x-yaml",
    )


def _revision(content: bytes) -> ConfigRevision:
    return ConfigRevision(hashlib.sha256(content).hexdigest())


def _fsync_directory(path: Path) -> None:
    """尽力把目录项更新刷盘；不支持目录 fsync 的平台安全忽略。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
