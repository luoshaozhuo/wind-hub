"""第二轮验收共享：三路径配置临时树（类 + 工厂）。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED = REPO_ROOT / "configs"


@dataclass(frozen=True, slots=True)
class ConfigTree:
    root: Path

    @property
    def service(self) -> Path:
        return self.root / "services" / "collector.yaml"

    @property
    def instances(self) -> Path:
        return self.root / "instances"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def devices_yaml(self) -> Path:
        return self.instances / "devices.yaml"

    @property
    def paths(self) -> Any:
        from core.infrastructure.config.collector_documents import ConfigPaths

        return ConfigPaths(self.service, self.instances, self.templates)

    def read(self, relative: str) -> dict[str, Any]:
        from core.infrastructure.config.yaml import read_yaml_mapping

        return read_yaml_mapping(self.root / relative)

    def write(self, relative: str, document: dict[str, Any]) -> None:
        from core.infrastructure.config.yaml import write_yaml_mapping_atomic

        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        write_yaml_mapping_atomic(target, document)


def make_config_tree(base: Path) -> ConfigTree:
    root = base / "cfg"
    for name in ("services", "instances", "templates"):
        shutil.copytree(SHIPPED / name, root / name)
    return ConfigTree(root)
