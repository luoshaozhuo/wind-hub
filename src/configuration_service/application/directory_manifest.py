"""Immutable directory-manifest contract for full configuration publication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from core.configuration.publication_identity import validate_relative_path


@dataclass(frozen=True, slots=True)
class DirectoryFile:
    relative_path: str
    yaml_bytes: bytes

    def __post_init__(self) -> None:
        validate_relative_path(self.relative_path)
        if not isinstance(self.yaml_bytes, bytes) or not self.yaml_bytes:
            raise ValueError("configuration YAML must be nonempty bytes")
        try:
            self.yaml_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("configuration YAML must be UTF-8") from exc


@dataclass(frozen=True, slots=True)
class DirectoryManifest:
    files: tuple[DirectoryFile, ...]
    sha256: str
    total_bytes: int


def build_directory_manifest(
    documents: tuple[DirectoryFile, ...],
    *,
    max_documents: int,
    max_bytes: int,
) -> DirectoryManifest:
    """Validate and hash an entire sorted publication snapshot.

    A manifest is a complete directory view, never a patch or a file-list delta.
    The digest binds path names and byte content; repeated requests are stable
    regardless of client input order. The caller decides bootstrap exclusions.
    """
    if max_documents < 1 or max_bytes < 1:
        raise ValueError("publication limits must be positive")
    if not documents:
        raise ValueError("empty full-directory publication is forbidden")
    if len(documents) > max_documents:
        raise ValueError("directory publication exceeds document count limit")
    if len({item.relative_path.casefold() for item in documents}) != len(documents):
        raise ValueError("duplicate configuration relative_path")
    ordered = tuple(sorted(documents, key=lambda item: item.relative_path))
    total = sum(len(item.yaml_bytes) for item in ordered)
    if total > max_bytes:
        raise ValueError("directory publication exceeds byte limit")
    payload = [
        {
            "relative_path": item.relative_path,
            "yaml_sha256": hashlib.sha256(item.yaml_bytes).hexdigest(),
        }
        for item in ordered
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return DirectoryManifest(ordered, hashlib.sha256(encoded).hexdigest(), total)
