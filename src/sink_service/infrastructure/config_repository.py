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

    def load(self, service_path: str) -> ValidatedSnapshot:
        """Read a complete committed generation through a single pointer."""
        if (
            not service_path.startswith("services/")
            or any(part in ("", ".", "..") for part in service_path.split("/"))
            or not service_path.endswith(".yaml")
        ):
            raise ConfigurationError("invalid Service relative path")
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
            raise RevisionConflictError("configuration revision conflict")
        checked = validate_snapshot(snapshot.files)
        if checked.revision != snapshot.revision:
            raise ConfigurationError("snapshot was modified after validation")
        if old is not None:
            previous = {item.path: item.content for item in old.files}
            current = {item.path: item.content for item in checked.files}
            for old_path, old_content in previous.items():
                if not old_path.startswith("templates/"):
                    continue
                if old_path not in current:
                    raise ConfigurationError(f"published template version deleted: {old_path}")
                if current[old_path] != old_content:
                    raise ConfigurationError(f"immutable template version modified: {old_path}")
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
        committed = read_snapshot(
            target,
            target
            / next(
                item.path
                for item in checked.files
                if item.path.startswith("services/") and item.path.endswith(".yaml")
            ),
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
