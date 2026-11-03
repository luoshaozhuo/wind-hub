"""Bounded and atomic export of complete remote configuration snapshots."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any


def export_snapshot(
    response: dict[str, Any],
    destination: Path,
    max_bytes: int,
) -> dict[str, Any]:
    """Export only verified relative files into an entirely new directory."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("output directory must not already exist")
    parent = destination.parent.resolve(strict=True)
    if not parent.is_dir() or destination.name in ("", ".", ".."):
        raise ValueError("invalid output destination")
    files = response.get("files")
    if not isinstance(files, list):
        raise ValueError("snapshot response has no files list")
    total = 0
    verified: list[tuple[Path, bytes]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("invalid snapshot file")
        name = item.get("relative_path")
        if not isinstance(name, str) or not name or "\\" in name:
            raise ValueError("invalid snapshot relative path")
        relative = Path(name)
        if (
            relative.is_absolute()
            or relative.as_posix() != name
            or any(part in (".", "..") for part in relative.parts)
        ):
            raise ValueError("snapshot path traversal refused")
        if name in seen:
            raise ValueError("duplicate snapshot file path")
        seen.add(name)
        encoded = item.get("content")
        if not isinstance(encoded, str):
            raise ValueError("snapshot file content missing")
        maximum_encoded = ((max_bytes - total + 2) // 3) * 4 + 4
        if len(encoded) > maximum_encoded:
            raise ValueError("snapshot encoded content exceeds configured limit")
        content = base64.b64decode(encoded, validate=True)
        declared_size = item.get("size_bytes", len(content))
        if int(declared_size) != len(content):
            raise ValueError("snapshot file size mismatch")
        checksum = item.get("sha256")
        if not isinstance(checksum, str) or hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError("snapshot checksum mismatch")
        total += len(content)
        if total > max_bytes:
            raise ValueError("snapshot exceeds configured max_bytes")
        verified.append((relative, content))
    with tempfile.TemporaryDirectory(prefix=".wind-hub-", dir=parent) as temporary:
        staging = Path(temporary) / "snapshot"
        staging.mkdir()
        for relative, content in verified:
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(staging, destination)
    return {
        "snapshot_id": response.get("snapshot_id", ""),
        "revision": response.get("revision", ""),
        "destination": str(destination),
        "file_count": len(verified),
        "total_bytes": total,
    }
