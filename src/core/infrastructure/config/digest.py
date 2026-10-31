"""Stable digest of shared configuration trees."""

from __future__ import annotations

import hashlib
from pathlib import Path


def config_dir_digest(config_dir: str | Path) -> str:
    """Hash nested YAML files by relative path and bytes in deterministic order."""
    directory = Path(config_dir)
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.yaml")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["config_dir_digest"]
