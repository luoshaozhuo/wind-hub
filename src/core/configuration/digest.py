"""Stable digest of shared configuration trees."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def config_dir_digest(config_dir: str | Path) -> str:
    """Hash YAML files by relative path and bytes; reject unsafe source trees."""
    directory = Path(config_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"configuration root must be a real directory: {directory}")

    files: list[Path] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, names in os.walk(
        directory, followlinks=False, onerror=raise_walk_error
    ):
        parent = Path(current)
        for name in directories:
            if (parent / name).is_symlink():
                raise ValueError(f"configuration directory contains a symlink: {parent / name}")
        for name in names:
            path = parent / name
            if path.suffix != ".yaml":
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"configuration YAML must be a regular file: {path}")
            files.append(path)

    if not files:
        raise ValueError(f"configuration directory contains no YAML files: {directory}")

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(directory).as_posix()):
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["config_dir_digest"]
