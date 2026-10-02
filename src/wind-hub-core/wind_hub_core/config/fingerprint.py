"""Wind Hub 配置集稳定指纹。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def fingerprint_config_set(config_dir: str | Path) -> str:
    """计算现场 YAML 配置集及同级 common/ 的稳定 SHA-256 指纹。"""
    site_dir = Path(config_dir).resolve()
    roots = [site_dir]
    common_dir = site_dir.parent / "common"
    if common_dir.is_dir() and common_dir != site_dir:
        roots.append(common_dir)

    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for root in roots:
        prefix = root.name
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}:
                relative = path.relative_to(root).as_posix()
                files.append((f"{prefix}/{relative}", path))

    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
