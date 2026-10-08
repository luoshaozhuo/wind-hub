"""Collector 配置集稳定指纹（与旧系统算法一致）。

对自包含现场配置目录计算 SHA-256：收集全部 ``.yaml`` / ``.yml`` 文件
（跳过 ``.history`` 目录），按相对路径排序，依次混入相对路径与文件
字节。算法与旧 ``fingerprint_config_set`` 完全一致，保证同一目录指纹
跨进程可比对。
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def fingerprint_config_set(config_dir: str | Path) -> str:
    """计算自包含现场配置目录的稳定 SHA-256 指纹。"""
    site_dir = Path(config_dir).resolve()
    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for path in site_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        relative_path = path.relative_to(site_dir)
        if ".history" in relative_path.parts:
            continue
        files.append((relative_path.as_posix(), path))

    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["fingerprint_config_set"]
