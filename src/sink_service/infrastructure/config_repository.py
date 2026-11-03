"""Crash-safe immutable configuration generation storage.

All managed files reside in immutable generation directories. Readers resolve a
single CURRENT pointer, so they cannot observe partially replaced file sets.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from sink_service.application.config import read_snapshot, validate_snapshot
from sink_service.domain.config import (
    ConfigurationError,
    RevisionConflictError,
    ValidatedSnapshot,
)


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

    def load(self, service_path: str, peer_service_path: str) -> ValidatedSnapshot:
        """Read a complete committed generation through a single pointer."""
        if (
            not service_path.startswith("configs/services/")
            or any(part in ("", ".", "..") for part in service_path.split("/"))
            or not service_path.endswith(".yaml")
        ):
            raise ConfigurationError("invalid Service relative path")
        identifier = self._current.read_text(encoding="ascii").strip()
        directory = self._generation(identifier)
        return read_snapshot(
            directory, directory / service_path, directory / peer_service_path
        )

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
            raise RevisionConflictError("configuration revision conflict")
        checked = validate_snapshot(snapshot.files)
        if checked.revision != snapshot.revision:
            raise ConfigurationError("snapshot was modified after validation")
        # The active snapshot contains only referenced templates. Removing a
        # reference does not delete its immutable record in older generations.
        current_templates = {
            item.path: item.content
            for item in checked.files
            if item.path.startswith("configs/templates/")
        }
        if old is not None:
            previous = {
                item.path: item.content
                for item in old.files
                if item.path.startswith("configs/templates/")
            }
            for path in previous.keys() & current_templates.keys():
                if previous[path] != current_templates[path]:
                    raise ConfigurationError(f"immutable template version modified: {path}")
        # A previously published version must never be reintroduced with
        # different bytes, even after it was unreferenced for several revisions.
        if self._generations.exists():
            for generation in self._generations.iterdir():
                if not generation.is_dir() or len(generation.name) != 64:
                    continue
                for path, content in current_templates.items():
                    published = generation / path
                    if published.is_file() and published.read_bytes() != content:
                        raise ConfigurationError(
                            f"immutable published template version modified: {path}"
                        )
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
        # Never point CURRENT at an existing corrupt or partial generation.
        # This also validates a generation reused after an interrupted Apply.
        service_paths = sorted(
            file.path for file in checked.files
            if file.path.startswith('configs/services/')
        )
        if len(service_paths) != 2:
            raise ConfigurationError('validated snapshot must contain two Services')
        committed = read_snapshot(
            target, target / service_paths[0], target / service_paths[1]
        )
        if committed.revision != checked.revision:
            raise ConfigurationError("existing committed generation differs from candidate")
        temporary = self._root / f".CURRENT-{secrets.token_hex(12)}"
        with temporary.open("x", encoding="ascii") as stream:
            stream.write(checked.revision + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._current)
        self._sync_dir(self._root)
        return checked.revision
