"""完整快照 YAML Adapter：版本目录 + 原子活跃版本切换。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from core.application.config_diff import diff_config_snapshots
from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError

from .parse_domain import load_domain
from .snapshot_writer import dump_snapshot
from .yaml import active_config_dir, read_yaml_mapping, write_yaml_mapping_atomic


def _sync_dir(path: Path) -> None:
    """Persist directory entries after atomic rename on POSIX."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class YamlConfigAdapter:
    """load() 读取单个一致版本；save() 验证后一次切换全部配置。"""

    _VERSION_ROOT = ".config-versions"
    _ACTIVE_FILE = ".active-config"

    def __init__(self, config_dir: str | Path) -> None:
        self._base = Path(config_dir)

    def _active_base(self) -> Path:
        return active_config_dir(self._base)

    def load(self) -> ConfigSnapshot:
        """只解析一个已发布的配置版本，不会读到跨文件混合状态。"""
        return load_domain(self._active_base())

    def save(self, snapshot: ConfigSnapshot) -> None:
        """原子发布经完整往返验证的快照；任何异常均保留旧版本。"""
        source = self._active_base()
        documents = dump_snapshot(snapshot)
        current_system = read_yaml_mapping(source / "system.yaml")
        documents["system.yaml"] = {**current_system, **documents["system.yaml"]}

        version_root = self._base / self._VERSION_ROOT
        version_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=version_root))
        generation = uuid4().hex
        final = version_root / generation
        try:
            for original in source.glob("*.yaml"):
                if original.name != "units.yaml":
                    shutil.copy2(original, staging / original.name)
            for filename, data in documents.items():
                write_yaml_mapping_atomic(staging / filename, data)
            reloaded = load_domain(staging)
            diff = diff_config_snapshots(snapshot, reloaded)
            if diff.has_changes:
                changed = tuple(
                    name for name, section in diff.sections.items()
                    if section.has_changes
                )
                raise ConfigError(
                    "Configuration cannot be saved losslessly: "
                    f"sections={changed}, site={diff.site_changed}, "
                    f"system={diff.system_changed}"
                )
            _sync_dir(staging)
            os.replace(staging, final)
            _sync_dir(version_root)
            pointer_tmp = version_root / f".pointer-{generation}"
            try:
                with pointer_tmp.open("w", encoding="ascii") as handle:
                    handle.write(generation + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(pointer_tmp, self._base / self._ACTIVE_FILE)
                _sync_dir(self._base)
            finally:
                pointer_tmp.unlink(missing_ok=True)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            # A failed pointer swap leaves a harmless unreferenced generation.
            # Keep old generations: concurrent readers may still use them.
