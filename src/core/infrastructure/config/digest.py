"""当前已发布配置的稳定 SHA-256 摘要。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .yaml import active_config_dir


def config_dir_digest(config_dir: str | Path) -> str:
    """只计算正在使用的 YAML 文件，不包括历史版本或备份。"""
    directory = active_config_dir(config_dir)
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.yaml"), key=lambda p: p.name):
        if path.name == "units.yaml":
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["config_dir_digest"]
