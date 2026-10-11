"""配置目录 YAML 文件的稳定 SHA-256 摘要。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def config_dir_digest(config_dir: str | Path) -> str:
    """计算配置目录中所有 YAML 文件的内容摘要。"""
    directory = Path(config_dir)
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.yaml"), key=lambda p: p.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["config_dir_digest"]
