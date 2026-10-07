"""基于单 YAML 文件的 Shared Core 配置仓储。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from core.application import ConfigError
from core.application.port import ConfigRepositoryPort
from core.domain import CoreConfigSnapshot

from .yaml_codec import YamlCoreConfigCodec


class YamlFileCoreConfigRepository(ConfigRepositoryPort):
    """使用单个 YAML 文件加载与原子保存 Shared Core 配置。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._codec = YamlCoreConfigCodec()
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> CoreConfigSnapshot:
        content = await asyncio.to_thread(self._read_bytes)
        return self._codec.decode(content)

    async def save(self, snapshot: CoreConfigSnapshot) -> None:
        content = self._codec.encode(snapshot)

        async with self._write_lock:
            await asyncio.to_thread(self._save_sync, content)

    def _read_bytes(self) -> bytes:
        try:
            return self._path.read_bytes()
        except FileNotFoundError as exc:
            raise ConfigError(
                f"core config file not found: {self._path}"
            ) from exc

    def _save_sync(self, content: bytes) -> None:
        mode = self._path.stat().st_mode & 0o777 if self._path.exists() else 0o644
        self._atomic_replace(content, mode=mode)

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


def _fsync_directory(path: Path) -> None:
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
