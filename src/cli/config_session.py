"""CLI 配置会话：完整校验、原子重载和快照差异。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.application.config_diff import ConfigSnapshotDiff, diff_config_snapshots
from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.infrastructure.config.adapter import YamlConfigAdapter

REQUIRED_FILES = (
    "system.yaml",
    "units.yaml",
    "device_models.yaml",
    "devices.yaml",
    "points.yaml",
    "tasks.yaml",
    "sinks.yaml",
    "business_points.yaml",
)


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """经完整验证、可供命令共享的配置快照。"""

    path: Path
    snapshot: ConfigSnapshot
    fingerprint: str
    loaded_at: datetime


class ConfigSession:
    """单一有效快照；任何失败不得部分替换当前配置。"""

    def __init__(self) -> None:
        self._current: LoadedConfig | None = None
        self._reload_failed = False

    @property
    def current(self) -> LoadedConfig:
        if self._current is None:
            raise ConfigError("configuration is not loaded; run config read PATH")
        return self._current

    @property
    def reload_failed(self) -> bool:
        return self._reload_failed

    @staticmethod
    def _files(path: Path) -> tuple[Path, ...]:
        if not path.is_dir():
            raise ConfigError(f"configuration directory not found: {path}")
        missing = tuple(name for name in REQUIRED_FILES if not (path / name).is_file())
        if missing:
            raise ConfigError(f"missing required configuration files: {', '.join(missing)}")
        return tuple(path / name for name in REQUIRED_FILES)

    @classmethod
    def _validated(cls, path: str | Path) -> LoadedConfig:
        base = Path(path).expanduser().resolve()
        paths = cls._files(base)
        # 只取一个稳定的文件集合快照；避免验证期间文件更新后指纹错误。
        before = tuple(item.read_bytes() for item in paths)
        snapshot = YamlConfigAdapter(base).load()
        after = tuple(item.read_bytes() for item in paths)
        if before != after:
            raise ConfigError("configuration changed during validation; retry")
        digest = hashlib.sha256()
        for item, content in zip(paths, after, strict=True):
            digest.update(item.name.encode("utf-8"))
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return LoadedConfig(
            path=base,
            snapshot=snapshot,
            fingerprint=digest.hexdigest(),
            loaded_at=datetime.now(timezone.utc),
        )

    def validate(self, path: str | Path) -> LoadedConfig:
        """验证而不切换当前会话。"""
        return self._validated(path)

    def read(self, path: str | Path) -> LoadedConfig:
        """验证成功后一次替换会话快照。"""
        candidate = self._validated(path)
        self._current = candidate
        self._reload_failed = False
        return candidate

    def reload(self) -> LoadedConfig:
        """失败保留原快照，但禁止自动流水线继续使用旧配置。"""
        path = self.current.path
        try:
            return self.read(path)
        except Exception:
            self._reload_failed = True
            raise

    def diff(self, path: str | Path) -> ConfigSnapshotDiff:
        """对比当前内存快照与另一路径，不切换当前配置。"""
        return diff_config_snapshots(
            self.current.snapshot,
            self._validated(path).snapshot,
        )

    def require_diagnosable(self) -> LoadedConfig:
        if self._reload_failed:
            raise ConfigError("last config reload failed; reload valid config before diagnose")
        return self.current
