"""Crash-safe immutable configuration generation storage.

All managed files reside in immutable generation directories. Readers resolve a
single CURRENT pointer, so they cannot observe partially replaced file sets.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from sink_service.application.config import read_snapshot, validate_snapshot
from sink_service.domain.config import ConfigurationError, SnapshotFile, ValidatedSnapshot


class GenerationStore:
    """Single-writer configuration repository with a revision compare-and-swap."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._generations = root / ".generations"
        self._current = root / "CURRENT"

    def _generation(self, identifier: str) -> Path:
        if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
            raise ConfigurationError("invalid generation identifier")
        return self._generations / identifier

    def load(self, service_path: str) -> ValidatedSnapshot:
        """Read a complete committed generation through a single pointer."""
        identifier = self._current.read_text(encoding="ascii").strip()
        directory = self._generation(identifier)
        return read_snapshot(directory, directory / service_path)

    @staticmethod
    def _sync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def commit(
        self,
        snapshot: ValidatedSnapshot,
        expected_revision: str,
        *,
        old: ValidatedSnapshot | None,
    ) -> str:
        """Commit only if the caller owns the sole writer and revision lock.

        This method requires external serialization via the configuration Writer.
        """
        current_revision = "" if old is None else old.revision
        if expected_revision != current_revision:
            raise ConfigurationError("configuration revision conflict")
        checked = validate_snapshot(snapshot.files)
        if checked.revision != snapshot.revision:
            raise ConfigurationError("snapshot was modified after validation")
        if old is not None:
            previous = {item.path: item.content for item in old.files}
            for item in checked.files:
                if item.path.startswith("templates/"):
                    old_content = previous.get(item.path)
                    if old_content is not None and old_content != item.content:
                        raise ConfigurationError(f"immutable template version modified: {item.path}")
        target = self._generation(checked.revision)
        self._generations.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            stage = self._generations / f".stage-{secrets.token_hex(12)}"
            stage.mkdir(mode=0o700)
            try:
                for item in checked.files:
                    path = stage / item.path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as stream:
                        stream.write(item.content)
                        stream.flush()
                        os.fsync(stream.fileno())
                for directory in sorted(
                    (p for p in stage.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts),
                    reverse=True,
                ):
                    self._sync_dir(directory)
                self._sync_dir(stage)
                os.rename(stage, target)
                self._sync_dir(self._generations)
            except BaseException:
                # A failed stage is not reachable through CURRENT.
                raise
        temporary = self._root / f".CURRENT-{secrets.token_hex(12)}"
        with temporary.open("x", encoding="ascii") as stream:
            stream.write(checked.revision + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._current)
        self._sync_dir(self._root)
        return checked.revision


def snapshot_from_wire(files: list[tuple[str, bytes, int, str]]) -> ValidatedSnapshot:
    """Validate metadata before interpreting caller-provided YAML bytes."""
    import hashlib

    items: list[SnapshotFile] = []
    for path, content, size, expected_hash in files:
        if size != len(content):
            raise ConfigurationError(f"{path}: size_bytes mismatch")
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ConfigurationError(f"{path}: sha256 mismatch")
        items.append(SnapshotFile(path, content))
    return validate_snapshot(tuple(items))
