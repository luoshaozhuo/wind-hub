"""Collector configuration contracts shared across application and adapters."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class SnapshotError(ValueError):
    """Invalid configuration candidate or snapshot operation."""


class SnapshotConflictError(SnapshotError):
    """The active configuration revision has changed."""


@dataclass(frozen=True, slots=True)
class SnapshotPaths:
    service: Path
    instances: Path
    templates: Path
    site: Path | None = None
    devices: Path | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    base_revision: str
    content_hash: str
    files: tuple[tuple[str, bytes], ...]
    expires_at: float


@dataclass(frozen=True, slots=True)
class CommitResult:
    revision: str
    paths: SnapshotPaths


def snapshot_digest(files: Mapping[str, bytes]) -> str:
    """Canonical byte-level digest of a complete effective file collection."""
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        encoded = path.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
