"""Data Generator private snapshots with validated, recoverable file commits."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.infrastructure.config.shared_documents import SharedConfigPaths, SharedDocumentLoader
from data_generator.domain.model import GeneratorError
from data_generator.domain.scenario import Settings
from data_generator.infrastructure.config import load_settings


@dataclass(frozen=True, slots=True)
class FileContent:
    path: str
    data: bytes


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(path.parent)


def _relative(root: Path, path: Path) -> str:
    root = root.resolve()
    candidate = path.resolve()
    if not candidate.is_relative_to(root):
        raise GeneratorError("configuration path escapes root")
    return candidate.relative_to(root).as_posix()


def _safe_target(root: Path, relative: str) -> Path:
    item = Path(relative)
    if (
        not relative
        or "\\" in relative
        or item.is_absolute()
        or ".." in item.parts
        or item.suffix != ".yaml"
    ):
        raise GeneratorError(f"invalid configuration path: {relative}")
    target = (root / item).resolve()
    if not target.is_relative_to(root.resolve()):
        raise GeneratorError("configuration path escape through symlink")
    return target


def _stage_readonly_tree(root: Path, destination: Path) -> None:
    """Copy bounded YAML sources from any configured root-relative subtree."""
    count = 0
    size = 0
    traversed = 0
    for source in root.rglob("*"):
        if ".dg_transaction" in source.relative_to(root).parts:
            continue
        traversed += 1
        if traversed > 20000:
            raise GeneratorError("configuration tree entry limit exceeded")
        if source.is_symlink():
            raise GeneratorError(f"symbolic link in configuration tree: {source}")
        if not source.is_file() or source.suffix != ".yaml":
            continue
        count += 1
        size += source.stat().st_size
        if count > 4096 or size > 64 * 1024 * 1024:
            raise GeneratorError("configuration staging resource limit exceeded")
        relative = source.relative_to(root).as_posix()
        target = _safe_target(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def recover_transaction(root: Path) -> None:
    """Recover an interrupted private-file commit before reading service settings."""
    root = root.resolve()
    journal = root / ".dg_transaction"
    marker = journal / "active.json"
    # A durable commit decision wins even if post-commit cleanup was interrupted.
    if (journal / "committed.json").exists():
        shutil.rmtree(journal)
        _sync_dir(root)
        return
    if marker.exists():
        state = json.loads(marker.read_text(encoding="utf-8"))
        for relative in state["changed"]:
            target = _safe_target(root, relative)
            backup = journal / "backup" / relative
            if relative in state["existing"]:
                if not backup.is_file():
                    raise GeneratorError(f"missing durable recovery backup: {relative}")
                restore = target.with_name(f".{target.name}.dg-restore")
                _durable_write(restore, backup.read_bytes())
                os.replace(restore, target)
            elif target.exists():
                target.unlink()
            _sync_dir(target.parent)
    if journal.exists():
        shutil.rmtree(journal)
        _sync_dir(root)


class SnapshotManager:
    """Core references are readable but never writable by Data Generator."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runtime_files = self._read_disk()
        self._disk_private = {
            file.path: file.data
            for file in self._runtime_files if file.path in self._owned()
        }
        self._candidate: tuple[str, tuple[FileContent, ...], str, Settings] | None = None
        self._restart_pending = False
        self.max_files = 2
        self.max_bytes = 8 * 1024 * 1024

    def _owned(self) -> set[str]:
        return {
            _relative(self.settings.root, self.settings.service_file),
            _relative(self.settings.root, self.settings.scenario_file),
        }

    def _read_disk(self) -> tuple[FileContent, ...]:
        settings = self.settings
        paths = {
            settings.service_file, settings.scenario_file,
            settings.site_file, settings.devices_file,
            settings.units_file, settings.business_points_file,
        }
        documents = SharedDocumentLoader(
            SharedConfigPaths(
                instances=settings.site_file.parent,
                templates=settings.templates_root,
                site_file=settings.site_file,
                devices_file=settings.devices_file,
            )
        ).load()
        for device in documents.devices.values():
            refs = device.templates
            paths.update((
                refs.model.path, refs.protocol.path, refs.point_table.path,
                *(item.path for item in refs.inherited_tables),
            ))
        return tuple(
            FileContent(_relative(settings.root, item), item.read_bytes())
            for item in sorted(paths)
        )

    def get(self, scope: str, candidate_id: str = "") -> tuple[FileContent, ...]:
        if scope == "CANDIDATE":
            if self._candidate is None or self._candidate[0] != candidate_id:
                raise GeneratorError("candidate not found")
            return self._candidate[1]
        if scope == "DISK":
            return self._read_disk()
        if scope == "RUNTIME":
            return self._runtime_files
        raise GeneratorError(f"unsupported snapshot scope: {scope}")

    def disk_revision(self) -> str:
        return load_settings(self.settings.service_file).revision

    def _assert_unchanged(self) -> Settings:
        latest = load_settings(self.settings.service_file)
        if latest.revision != self.settings.revision:
            raise GeneratorError("configuration changed since Push; resubmit candidate")
        return latest

    def _adopt_authoritative_shared(self) -> None:
        """Refresh Core-owned data without accepting externally edited private files."""
        current = load_settings(self.settings.service_file)
        for relative, original in self._disk_private.items():
            if _safe_target(self.settings.root, relative).read_bytes() != original:
                raise GeneratorError("private configuration changed outside Data Generator")
        self.settings = current

    def push(self, files: tuple[FileContent, ...], expected: str) -> str:
        self._adopt_authoritative_shared()
        if not expected or expected != self.settings.revision:
            raise GeneratorError("expected_revision mismatch")
        if len(files) != self.max_files:
            raise GeneratorError("snapshot must include exactly two private files")
        if sum(len(f.data) for f in files) > self.max_bytes:
            raise GeneratorError("snapshot size limit exceeded")
        supplied = {f.path for f in files}
        if len(supplied) != len(files) or supplied != self._owned():
            raise GeneratorError("snapshot may only update owned service and scenario files")
        if any(not f.data for f in files):
            raise GeneratorError("empty private configuration document")

        # Stage against the complete *read-only* Core tree, never publish it.
        root = self.settings.root
        with tempfile.TemporaryDirectory(prefix="dg-config-") as scratch:
            stage = Path(scratch) / "configs"
            _stage_readonly_tree(root, stage)
            for file in files:
                target = _safe_target(stage, file.path)
                _durable_write(target, file.data)
            staged = load_settings(
                stage / _relative(root, self.settings.service_file)
            )
            candidate_document = yaml.safe_load(staged.scenario_file.read_text(encoding="utf-8"))
        old_document = yaml.safe_load(
            self.settings.scenario_file.read_text(encoding="utf-8")
        )
        existing = {
            item["scenario_id"]: item for item in old_document["scenarios"]
        }
        for item in candidate_document["scenarios"]:
            before = existing.get(item["scenario_id"])
            if (
                before is not None
                and before["version"] == item["version"]
                and before != item
            ):
                raise GeneratorError(
                    "immutable scenario version changed: "
                    f"{item['scenario_id']}@{item['version']}"
                )
        candidate_id = str(uuid.uuid4())
        self._candidate = (candidate_id, files, self.settings.revision, staged)
        return candidate_id

    def apply(self, candidate_id: str, expected: str) -> tuple[Settings, bool]:
        candidate = self._candidate
        if candidate is None or candidate_id != candidate[0]:
            raise GeneratorError("candidate not found")
        if not expected or expected != candidate[2]:
            raise GeneratorError("expected_revision mismatch")
        self._assert_unchanged()
        root = self.settings.root
        original = {
            f.path: f.data for f in self._read_disk()
            if f.path in self._owned()
        }
        proposed = {f.path: f.data for f in candidate[1]}
        changed = [name for name in sorted(proposed) if proposed[name] != original[name]]
        if not changed:
            if not self._restart_pending:
                # A no-op private Apply can still adopt newer Core-owned metadata.
                self._runtime_files = self._read_disk()
            self._candidate = None
            return self.settings, self._restart_pending

        # The API lock hides the multi-file update from service clients.
        # Recovery journal restores the prior complete private snapshot after
        # an interrupted commit. Direct filesystem readers are not isolated.
        journal = root / ".dg_transaction"
        if (journal / "committed.json").exists():
            recover_transaction(root)
        if journal.exists():
            raise GeneratorError("unfinished file transaction; recover on restart")
        journal.mkdir()
        try:
            for name in changed:
                _durable_write(journal / "backup" / name, original[name])
                _durable_write(journal / "new" / name, proposed[name])
            _durable_write(
                journal / "active.json",
                json.dumps({"existing": changed, "changed": changed}).encode(),
            )
            _sync_dir(journal)
            for name in changed:
                target = _safe_target(root, name)
                os.replace(journal / "new" / name, target)
                _sync_dir(target.parent)
            new_settings = load_settings(self.settings.service_file)
            if new_settings.revision != candidate[3].revision:
                raise GeneratorError("candidate changed between validation and commit")
            restart = (
                proposed[_relative(root, self.settings.service_file)]
                != original[_relative(root, self.settings.service_file)]
            )
            # Durable decision separates successful commit from rollback recovery.
            _durable_write(
                journal / "committed.json",
                json.dumps({"revision": new_settings.revision}).encode(),
            )
        except BaseException:
            recover_transaction(root)
            raise
        else:
            # Cleanup failures do not transform a committed update into failure.
            try:
                shutil.rmtree(journal)
                _sync_dir(root)
            except OSError:
                pass
            self.settings = new_settings
            self._disk_private = {
                file.path: file.data for file in self._read_disk()
                if file.path in self._owned()
            }
            self._restart_pending = self._restart_pending or restart
            if not self._restart_pending:
                self._runtime_files = self._read_disk()
            self._candidate = None
            return new_settings, self._restart_pending
