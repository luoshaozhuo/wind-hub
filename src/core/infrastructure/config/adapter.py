"""完整配置快照的 YAML 读写适配器。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from core.application.config_diff import diff_config_snapshots
from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError

from .parse_domain import load_domain
from .snapshot_writer import dump_snapshot
from .yaml import read_yaml_mapping, write_yaml_mapping_atomic


class YamlConfigAdapter:
    """只负责 YAML 与配置快照之间的读写转换。"""

    def __init__(self, config_dir: str | Path) -> None:
        self._base = Path(config_dir)

    def load(self) -> ConfigSnapshot:
        return load_domain(self._base)

    def save(self, snapshot: ConfigSnapshot) -> None:
        documents = dump_snapshot(snapshot)
        current_system = read_yaml_mapping(self._base / "system.yaml")
        external = {
            key: value for key, value in current_system.items()
            if key not in {"site", "runtime", "ads"}
        }
        documents["system.yaml"] = {**external, **documents["system.yaml"]}

        # 在任何正式文件被替换前，验证整套配置可读且往返无损。
        with tempfile.TemporaryDirectory(dir=self._base) as directory:
            staging = Path(directory)
            for original in self._base.glob("*.yaml"):
                shutil.copy2(original, staging / original.name)
            for filename, document in documents.items():
                write_yaml_mapping_atomic(staging / filename, document)
            restored = load_domain(staging)
            if diff_config_snapshots(snapshot, restored).has_changes:
                raise ConfigError("Configuration cannot be saved losslessly")

        originals = {
            filename: (self._base / filename).read_bytes()
            if (self._base / filename).exists() else None
            for filename in documents
        }
        written: list[str] = []
        try:
            for filename, document in documents.items():
                write_yaml_mapping_atomic(self._base / filename, document)
                written.append(filename)
        except Exception:
            for filename in reversed(written):
                target = self._base / filename
                original = originals[filename]
                if original is None:
                    target.unlink(missing_ok=True)
                    continue
                with tempfile.NamedTemporaryFile(dir=self._base, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            raise
