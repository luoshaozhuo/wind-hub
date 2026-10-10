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
from .yaml import read_yaml_mapping, write_yaml_mapping_atomic


class YamlConfigAdapter:
    """load() 读取单个一致版本；save() 验证后一次切换全部配置。"""

    _VERSION_ROOT = ".config-versions"
    _ACTIVE_FILE = ".active-config"

    def __init__(self, config_dir: str | Path) -> None:
        self._base = Path(config_dir)

    def _active_base(self) -> Path:
        pointer = self._base / self._ACTIVE_FILE
        if not pointer.exists():
            return self._base
        generation = pointer.read_text(encoding="ascii").strip()
        if not generation or len(generation) != 32 or any(
            symbol not in "0123456789abcdef" for symbol in generation
        ):
            raise ConfigError("Invalid active configuration generation")
        version = self._base / self._VERSION_ROOT / generation
        if not version.is_dir():
            raise ConfigError(f"Active configuration version is missing: {generation}")
        return version

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
        published = False
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
            os.replace(staging, final)
            pointer_tmp = version_root / f".pointer-{generation}"
            try:
                with pointer_tmp.open("w", encoding="ascii") as handle:
                    handle.write(generation + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(pointer_tmp, self._base / self._ACTIVE_FILE)
            finally:
                pointer_tmp.unlink(missing_ok=True)
            published = True
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            # A failed pointer swap leaves a harmless unreferenced generation.
            # Keep old generations: concurrent readers may still use them.
