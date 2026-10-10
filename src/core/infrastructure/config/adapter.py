"""全量配置读写适配器。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from shutil import copy2

from core.application.config_snapshot import ConfigSnapshot
from core.application.config_diff import diff_config_snapshots
from core.application.errors import ConfigError

from .parse_domain import load_domain
from .snapshot_writer import dump_snapshot
from .yaml import read_yaml_mapping, write_yaml_mapping_atomic


class YamlConfigAdapter:
    """只暴露完整 ConfigSnapshot 的 load/save，不含按主题接口。"""

    def __init__(self, config_dir: str | Path) -> None:
        self._base = Path(config_dir)

    def load(self) -> ConfigSnapshot:
        return load_domain(self._base)

    def save(self, snapshot: ConfigSnapshot) -> None:
        """先在临时目录完整验证，避免将不等价快照写回配置目录。"""
        documents = dump_snapshot(
            snapshot,
            read_yaml_mapping(self._base / "device_models.yaml"),
        )
        original = read_yaml_mapping(self._base / 'system.yaml')
        documents['system.yaml'] = {**original, **documents['system.yaml']}
        with TemporaryDirectory(prefix="wind-hub-config-") as directory:
            staging = Path(directory)
            for path in self._base.glob("*.yaml"):
                copy2(path, staging / path.name)
            for filename, data in documents.items():
                write_yaml_mapping_atomic(staging / filename, data)
            restored = YamlConfigAdapter(staging).load()
            difference = diff_config_snapshots(snapshot, restored)
            if difference.has_changes:
                raise ConfigError(
                    "Cannot save configuration losslessly: "
                    f"changed sections={tuple(name for name, part in difference.sections.items() if part.has_changes)}, "
                    f"site_changed={difference.site_changed}, system_changed={difference.system_changed}"
                )
            # Keep a recoverable copy of every overwritten file.
            with TemporaryDirectory(prefix="wind-hub-backup-") as backup_dir:
                backup = Path(backup_dir)
                for filename in documents:
                    copy2(self._base / filename, backup / filename)
                try:
                    for filename, data in documents.items():
                        write_yaml_mapping_atomic(self._base / filename, data)
                except BaseException:
                    # Restore all originals, including files overwritten before failure.
                    for filename in documents:
                        copy2(backup / filename, self._base / filename)
                    raise
