"""Crash-safe immutable configuration generation storage.

All managed files reside in immutable generation directories. Readers resolve a
single CURRENT pointer, so they cannot observe partially replaced file sets.
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from sink_service.domain.config import (
    ConfigurationError,
    RevisionConflictError,
    SnapshotFile,
    ValidatedSnapshot,
)
from sink_service.infrastructure.config_io import (
    mutate_delivery,
    snapshot_from_wire,
    validate_snapshot,
)


class GenerationStore:
    """Single-writer configuration repository with a revision compare-and-swap."""

    @staticmethod
    def validate_snapshot(files: tuple[SnapshotFile, ...]) -> ValidatedSnapshot:
        return validate_snapshot(files)

    @staticmethod
    def snapshot_from_wire(files: list[tuple[str, bytes, int, str]]) -> ValidatedSnapshot:
        return snapshot_from_wire(files)

    @staticmethod
    def mutate_delivery(
        file: SnapshotFile, delivery_id: str, *, running: bool, revision: str
    ) -> SnapshotFile:
        return mutate_delivery(file, delivery_id, running=running, revision=revision)

    def __init__(self, root: Path) -> None:
        self._root = root
        self._generations = root / ".generations"
        self._current = root / "CURRENT"

    def _generation(self, identifier: str) -> Path:
        if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
            raise ConfigurationError("invalid generation identifier")
        return self._generations / identifier

    @staticmethod
    def _read_generation(directory: Path, service_path: str) -> ValidatedSnapshot:
        """Validate a committed snapshot without reselecting local Deployment.

        The generation contains the already selected Service/Instance/Template
        closure; Deployment selection is performed only at initial bootstrap.
        """
        files: list[SnapshotFile] = []
        for path in sorted(directory.rglob("*.yaml")):
            if path.is_symlink() or not path.is_file():
                raise ConfigurationError("unsafe committed generation file")
            relative = path.relative_to(directory).as_posix()
            if not relative.startswith(
                ("configs/services/", "configs/instances/", "configs/templates/")
            ):
                raise ConfigurationError("unexpected committed generation path")
            files.append(SnapshotFile(relative, path.read_bytes()))
        services = [
            item.path for item in files if item.path.startswith("configs/services/")
        ]
        if services != [service_path]:
            raise ConfigurationError("committed generation Service selection mismatch")
        return validate_snapshot(tuple(files))

    def selected_service_path(self) -> str:
        """Return the sole Service selected by the persisted CURRENT generation.

        The caller's bootstrap filename is not authoritative after a published
        Deployment switches to another immutable Service file version.
        """
        identifier = self._current.read_text(encoding="ascii").strip()
        directory = self._generation(identifier)
        if directory.is_symlink() or not directory.is_dir():
            raise ConfigurationError("missing or unsafe committed generation")
        service_root = directory / "configs" / "services"
        if service_root.is_symlink() or not service_root.is_dir():
            raise ConfigurationError("missing or unsafe committed Service directory")
        names = [
            path.name
            for path in service_root.iterdir()
            if path.is_file() and not path.is_symlink() and path.name.endswith(".yaml")
        ]
        if len(names) != 1 or not names[0].startswith("service_"):
            raise ConfigurationError("committed generation has no unique versioned Service")
        return "configs/services/" + names[0]

    def load(self, service_path: str) -> ValidatedSnapshot:
        """Read a complete committed generation through a single pointer."""
        if (
            not service_path.startswith("configs/services/")
            or any(part in ("", ".", "..") for part in service_path.split("/"))
            or not service_path.endswith(".yaml")
        ):
            raise ConfigurationError("invalid Service relative path")
        identifier = self._current.read_text(encoding="ascii").strip()
        directory = self._generation(identifier)
        snapshot = self._read_generation(directory, service_path)
        if snapshot.revision != identifier:
            raise ConfigurationError("committed generation content hash mismatch")
        return snapshot

    def load_generation(self, revision: str, service_path: str) -> ValidatedSnapshot:
        """Load an exact immutable generation for a running Worker process."""
        if (
            not service_path.startswith("configs/services/service_")
            or not service_path.endswith(".yaml")
            or any(part in ("", ".", "..") for part in service_path.split("/"))
        ):
            raise ConfigurationError("invalid Service path")
        directory = self._generation(revision)
        if directory.is_symlink() or not directory.is_dir():
            raise ConfigurationError("missing or unsafe requested generation")
        snapshot = self._read_generation(directory, service_path)
        if snapshot.revision != revision:
            raise ConfigurationError("requested generation digest mismatch")
        return snapshot

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
                    output_path = stage / item.path
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with output_path.open("xb") as stream:
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
                # Failed or interrupted staging must not accumulate unbounded
                # directories; CURRENT never points to an incomplete stage.
                if stage.exists():
                    shutil.rmtree(stage)
                raise
        # Never point CURRENT at an existing corrupt or partial generation.
        # This also validates a generation reused after an interrupted Apply.
        service_paths = sorted(
            file.path for file in checked.files if file.path.startswith("configs/services/")
        )
        if len(service_paths) != 1 or not service_paths[0].startswith(
            "configs/services/service_"
        ) or not service_paths[0].endswith(".yaml"):
            raise ConfigurationError("validated snapshot requires one versioned Service")
        committed = self._read_generation(target, service_paths[0])
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
