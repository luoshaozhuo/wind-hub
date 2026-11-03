"""Validated configuration snapshots with crash-recoverable file transaction."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from data_generator.domain.scenario import Settings
from data_generator.infrastructure.config import load_settings
from data_generator.domain.model import GeneratorError


@dataclass(frozen=True, slots=True)
class FileContent:
    path: str
    data: bytes


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(path.parent)


def _validate_path(path: str) -> None:
    if not path or "\\" in path or Path(path).is_absolute() or "//" in path:
        raise GeneratorError("invalid snapshot path")
    parts = Path(path).parts
    if any(part in (".", "..") for part in parts):
        raise GeneratorError("snapshot path traversal")
    if path not in (
        "services/data-generator-service.yaml",
        "instances/data-generator.yaml",
        "instances/site.yaml",
        "instances/devices.yaml",
        "templates/units.yaml",
        "templates/business_points.yaml",
    ) and not (len(parts) >= 3 and parts[0] == "templates"
                and parts[-1].endswith(".yaml")):
        raise GeneratorError(f"unmanaged snapshot path: {path}")


def _check_file_path(root: Path, relative: str) -> Path:
    _validate_path(relative)
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise GeneratorError(f"snapshot path escaped root: {relative}")
    return target


def recover_transaction(root: Path) -> None:
    """Rollback any interrupted file-set commit before loading configuration."""
    journal = root / ".dg_transaction"
    marker = journal / "active.json"
    if marker.exists():
        state = json.loads(marker.read_text(encoding="utf-8"))
        for relative in state["new_files"]:
            target = _check_file_path(root, relative)
            backup = journal / "backup" / relative
            if relative in state["old_files"]:
                if not backup.is_file():
                    raise GeneratorError(f"missing recovery backup: {relative}")
                temporary = target.with_name(f".{target.name}.dg-recover")
                _write_durable(temporary, backup.read_bytes())
                os.replace(temporary, target)
                _sync_dir(target.parent)
            elif target.exists():
                target.unlink()
                _sync_dir(target.parent)
    if journal.exists():
        shutil.rmtree(journal)
        _sync_dir(root)


class SnapshotManager:
    """Owns the in-memory candidate and authoritative disk snapshot boundary."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runtime_files = self._read_disk()
        self._candidate: tuple[str, tuple[FileContent, ...], str, Settings] | None = None
        self.max_files = 128
        self.max_bytes = 8 * 1024 * 1024

    def _read_disk(self) -> tuple[FileContent, ...]:
        root = self.settings.root
        files = [root / "services" / "data-generator-service.yaml"]
        files.extend(sorted((root / "instances").glob("*.yaml")))
        files.extend(sorted((root / "templates").rglob("*.yaml")))
        result: list[FileContent] = []
        for file in files:
            relative = file.relative_to(root).as_posix()
            result.append(FileContent(relative, _check_file_path(root, relative).read_bytes()))
        return tuple(result)

    def get(self, scope: str, candidate_id: str = "") -> tuple[FileContent, ...]:
        if scope == "CANDIDATE":
            if self._candidate is None or candidate_id != self._candidate[0]:
                raise GeneratorError("candidate not found")
            return self._candidate[1]
        if scope == "DISK":
            return self._read_disk()
        if scope == "RUNTIME":
            return self._runtime_files
        raise GeneratorError("unknown snapshot scope")

    def push(self, files: tuple[FileContent, ...], expected: str) -> str:
        if expected and expected != self.settings.revision:
            raise GeneratorError("snapshot revision conflict")
        if not files or len(files) > self.max_files:
            raise GeneratorError("snapshot file count exceeded")
        if sum(len(file.data) for file in files) > self.max_bytes:
            raise GeneratorError("snapshot byte capacity exceeded")
        paths: set[str] = set()
        for file in files:
            if file.path in paths or not file.data:
                raise GeneratorError("duplicate path or empty snapshot content")
            paths.add(file.path)
            _check_file_path(self.settings.root, file.path)
        required = {
            "services/data-generator-service.yaml",
            "instances/data-generator.yaml",
            "instances/site.yaml",
            "instances/devices.yaml",
            "templates/units.yaml",
            "templates/business_points.yaml",
        }
        if not required <= paths:
            raise GeneratorError("snapshot missing required config documents")
        with tempfile.TemporaryDirectory(prefix="dg-validate-") as location:
            staged = Path(location)
            for file in files:
                target = _check_file_path(staged, file.path)
                _write_durable(target, file.data)
            staged_settings = load_settings(
                staged / "services" / "data-generator-service.yaml"
            )
        for scenario_id, previous in self.settings.scenarios.items():
            changed = staged_settings.scenarios.get(scenario_id)
            if changed is not None and changed.version == previous.version and changed != previous:
                raise GeneratorError(
                    f"scenario version {scenario_id}@{previous.version} cannot be overwritten"
                )
        original = {item.path: item.data for item in self._read_disk()}
        for item in files:
            if item.path.startswith("templates/") and item.path in original:
                if item.data != original[item.path]:
                    raise GeneratorError(
                        f"published template is immutable: {item.path}"
                    )
        candidate_id = str(uuid.uuid4())
        self._candidate = (candidate_id, files, self.settings.revision, staged_settings)
        return candidate_id

    def apply(self, candidate_id: str, expected: str) -> tuple[Settings, bool]:
        if self._candidate is None or self._candidate[0] != candidate_id:
            raise GeneratorError("candidate not found")
        _, files, base_revision, _ = self._candidate
        if expected and expected != base_revision:
            raise GeneratorError("snapshot revision conflict")
        if base_revision != self.settings.revision:
            raise GeneratorError("candidate baseline is stale")
        root = self.settings.root
        journal = root / ".dg_transaction"
        if journal.exists():
            raise GeneratorError("unfinished transaction: restart for recovery")
        original = {file.path: file.data for file in self._read_disk()}
        proposed = {file.path: file.data for file in files}
        if set(original) - set(proposed):
            raise GeneratorError("snapshot cannot remove existing config documents")
        journal.mkdir()
        try:
            for relative, data in original.items():
                _write_durable(journal / "backup" / relative, data)
            for relative, data in proposed.items():
                _write_durable(journal / "new" / relative, data)
            state = {"old_files": sorted(original), "new_files": sorted(proposed)}
            _write_durable(journal / "active.json", json.dumps(state).encode())
            _sync_dir(journal)
            for relative in sorted(proposed):
                target = _check_file_path(root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = journal / "new" / relative
                os.replace(temporary, target)
                _sync_dir(target.parent)
            new_settings = load_settings(
                root / "services" / "data-generator-service.yaml"
            )
            new_service = proposed["services/data-generator-service.yaml"]
            old_service = original["services/data-generator-service.yaml"]
            restart_required = new_service != old_service
            (journal / "active.json").unlink()
            _sync_dir(journal)
        except Exception:
            recover_transaction(root)
            raise
        else:
            shutil.rmtree(journal)
            self.settings = new_settings
            if not restart_required:
                self._runtime_files = files
            else:
                old_runtime = {file.path: file.data for file in self._runtime_files}
                for file in files:
                    if file.path != "services/data-generator-service.yaml":
                        old_runtime[file.path] = file.data
                self._runtime_files = tuple(FileContent(p, data)
                                            for p, data in sorted(old_runtime.items()))
            self._candidate = None
            return new_settings, restart_required
