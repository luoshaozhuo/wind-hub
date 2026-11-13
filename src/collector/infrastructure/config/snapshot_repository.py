"""Collector-owned immutable configuration generations.

Core remains responsible for YAML and domain validation. This module owns only
Collector's candidate lifecycle and the atomic selection of a file generation.
No protocol connection or runtime action occurs while storing a candidate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from collector.application.config_models import (
    Candidate,
    CommitResult,
    snapshot_digest,
)
from collector.application.config_models import (
    SnapshotConflictError as SnapshotConflictError,
)
from collector.application.config_models import (
    SnapshotError as SnapshotError,
)
from collector.application.config_models import (
    SnapshotPaths as SnapshotPaths,
)

# Versioned templates are immutable once published. They are matched by
# category prefix rather than path depth: the shared flat layout
# (templates/<category>/<id>_<version>.yaml) has only two separators, while
# the legacy nested layout has three.
_PUBLISHED_TEMPLATE_PREFIXES = (
    "templates/protocols/",
    "templates/device_models/",
    "templates/point_tables/",
    "templates/tasks/",
)


class CollectorSnapshotRepository:
    """Versioned full snapshot storage with a single atomic pointer.

    The repository lives in an isolated storage root. The active generation is
    read through CURRENT, never through partially replaced source YAML files.
    External legacy paths may be imported once by the caller but are not edited.
    """

    def __init__(
        self,
        root: Path,
        *,
        candidate_capacity: int = 8,
        candidate_ttl: float = 3600.0,
        max_files: int = 256,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if min(candidate_capacity, candidate_ttl, max_files, max_bytes) <= 0:
            raise ValueError("snapshot limits must be positive")
        self._root = root.resolve()
        self._generations = self._root / "generations"
        self._pointer = self._root / "CURRENT"
        self._capacity = candidate_capacity
        self._ttl = candidate_ttl
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._pending: dict[str, Candidate] = {}
        self._lock = asyncio.Lock()
        self._storage_lock = threading.RLock()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._candidates = self._root / "candidates"
        self._candidates.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The process owns this state root exclusively at startup. Reclaim
        # interrupted staging directories before serving any new requests.
        with self._storage_lock:
            self._prune()

    @staticmethod
    def _normalize(relative_path: str) -> str:
        if (
            not relative_path
            or relative_path.startswith("/")
            or "\\" in relative_path
            or "\x00" in relative_path
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise SnapshotError("unsafe snapshot path")
        path = PurePosixPath(relative_path)
        parts = path.parts
        if parts == ("services", "collector.yaml"):
            return relative_path
        if parts in {("instances", "site.yaml"), ("instances", "devices.yaml")}:
            return relative_path
        if parts in {("templates", "units_standard.yaml"), ("templates", "business_points_standard.yaml")}:
            return relative_path
        # Template files live in a fixed category directory. Both the flat
        # shared-config layout (templates/<category>/<name>.yaml) and the
        # versioned nested layout (templates/<category>/<group>/<name>.yaml)
        # are owned; the category whitelist and suffix checks still bound the
        # snapshot to Collector-owned files.
        if (
            parts[0] == "templates"
            and parts[1] in {"protocols", "device_models", "point_tables", "tasks"}
            and len(parts) in {3, 4}
            and parts[-1].endswith(".yaml")
            and parts[-1] != ".yaml"
            and (len(parts) == 3 or parts[2])
        ):
            return relative_path
        raise SnapshotError("file outside Collector configuration ownership")

    @classmethod
    def _canonical(
        cls, files: Mapping[str, bytes], *, max_files: int, max_bytes: int
    ) -> tuple[tuple[tuple[str, bytes], ...], str]:
        if not files or len(files) > max_files:
            raise SnapshotError("snapshot file count exceeds limits")
        result: list[tuple[str, bytes]] = []
        total = 0
        for name, value in sorted(files.items()):
            path = cls._normalize(name)
            if not isinstance(value, bytes):
                raise SnapshotError("snapshot contents must be bytes")
            total += len(value)
            if total > max_bytes:
                raise SnapshotError("snapshot byte limit exceeded")
            result.append((path, value))
        required_instances = {
            "services/collector.yaml",
            "instances/site.yaml",
            "instances/devices.yaml",
        }
        if not required_instances.issubset(files):
            raise SnapshotError("snapshot missing required configuration files")
        if not {"templates/units_standard.yaml", "templates/business_points_standard.yaml"}.issubset(files):
            raise SnapshotError("snapshot missing shared catalogs")
        return tuple(result), snapshot_digest(dict(result))

    def _candidate_path(self, candidate_id: str) -> Path:
        if len(candidate_id) != 32 or any(
            value not in "0123456789abcdef" for value in candidate_id
        ):
            raise SnapshotError("invalid candidate identifier")
        return self._candidates / candidate_id

    def _persist_candidate(self, candidate: Candidate) -> None:
        destination = self._candidate_path(candidate.candidate_id)
        staging = Path(tempfile.mkdtemp(prefix=".candidate-", dir=self._candidates))
        try:
            self._write_generation(staging, candidate.files)
            manifest = {
                "candidate_id": candidate.candidate_id,
                "base_revision": candidate.base_revision,
                "content_hash": candidate.content_hash,
                "expires_at": time.time() + self._ttl,
            }
            manifest_path = staging / "MANIFEST.json"
            with manifest_path.open("w", encoding="utf-8") as stream:
                json.dump(manifest, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            self._sync_directory(staging)
            os.rename(staging, destination)
            self._sync_directory(self._candidates)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _load_candidate(self, candidate_id: str) -> Candidate:
        root = self._candidate_path(candidate_id)
        try:
            document = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
            if root.is_symlink() or document.get("candidate_id") != candidate_id:
                raise SnapshotError("candidate identity mismatch")
            base_revision = document.get("base_revision")
            if not isinstance(base_revision, str):
                raise SnapshotError("invalid candidate base revision")
            expiry = float(document["expires_at"])
            if not math.isfinite(expiry):
                raise SnapshotError("invalid candidate expiration")
            if expiry <= time.time():
                raise SnapshotError("candidate has expired")
            files: dict[str, bytes] = {}
            for path in root.rglob("*.yaml"):
                relative = path.relative_to(root).as_posix()
                self._normalize(relative)
                if path.is_symlink() or not path.is_file():
                    raise SnapshotError("unsafe candidate file")
                files[relative] = path.read_bytes()
            canonical, digest = self._canonical(
                files, max_files=self._max_files, max_bytes=self._max_bytes
            )
            if digest != document["content_hash"]:
                raise SnapshotError("candidate hash mismatch")
            return Candidate(
                candidate_id=candidate_id,
                base_revision=base_revision,
                content_hash=digest,
                files=canonical,
                expires_at=time.monotonic() + max(0.0, expiry - time.time()),
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            raise SnapshotError("candidate is unavailable or corrupted") from exc

    def _prune(self) -> None:
        # Callers hold the storage lock: temporary directories can only be
        # remnants of a previous process, never active writes in this process.
        for parent, prefix in (
            (self._root, ".validate-"),
            (self._candidates, ".candidate-"),
            (self._generations, ".generation-"),
        ):
            if not parent.is_dir():
                continue
            for path in parent.glob(f"{prefix}*"):
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
        now = time.monotonic()
        for key, item in tuple(self._pending.items()):
            if item.expires_at <= now:
                del self._pending[key]
                shutil.rmtree(self._candidate_path(key), ignore_errors=True)
        for path in self._candidates.iterdir():
            if path.name.startswith(".candidate-"):
                continue
            try:
                manifest = json.loads((path / "MANIFEST.json").read_text(encoding="utf-8"))
                expires_at = float(manifest["expires_at"])
                expired = not math.isfinite(expires_at) or expires_at <= time.time()
            except (OSError, ValueError, KeyError, TypeError):
                expired = True
            if expired:
                shutil.rmtree(path, ignore_errors=True)

    def collect_orphan_generations(self) -> int:
        """Remove unreferenced generations without deleting the active revision."""
        with self._storage_lock:
            active = self.active_revision()
            receipts = self._root / "receipts"
            referenced = {active} if active is not None else set()
            if receipts.is_dir():
                for path in receipts.glob("*.json"):
                    try:
                        document = json.loads(path.read_text(encoding="utf-8"))
                        referenced.add(str(document["revision"]))
                    except (OSError, ValueError, KeyError):
                        continue
            referenced.update(item.content_hash for item in self._pending.values())
            removed = 0
            if not self._generations.is_dir():
                return removed
            for path in self._generations.iterdir():
                if path.is_dir() and path.name not in referenced:
                    shutil.rmtree(path)
                    removed += 1
            return removed

    def active_revision(self) -> str | None:
        try:
            revision = self._pointer.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        if len(revision) != 64 or any(letter not in "0123456789abcdef" for letter in revision):
            raise SnapshotError("corrupt active configuration pointer")
        if not (self._generations / revision).is_dir():
            raise SnapshotError("active configuration generation does not exist")
        return revision

    def active_paths(self) -> SnapshotPaths:
        revision = self.active_revision()
        if revision is None:
            raise SnapshotError("no active configuration snapshot")
        self.read_generation(revision)
        return self._paths(self._generations / revision)

    @staticmethod
    def _paths(root: Path) -> SnapshotPaths:
        return SnapshotPaths(
            service=root / "services" / "collector.yaml",
            instances=root / "instances",
            templates=root / "templates",
            site=root / "instances" / "site.yaml",
            devices=root / "instances" / "devices.yaml",
        )

    def stage(
        self,
        files: Mapping[str, bytes],
        *,
        expected_revision: str,
        validate: Callable[[SnapshotPaths], None],
    ) -> Candidate:
        """Validate a complete candidate without changing CURRENT."""
        with self._storage_lock:
            self._prune()
            # Reclaim generation directories left by a crash in the window
            # between promotion and the durable receipt, so they cannot
            # accumulate unbounded across Push requests after a restart.
            self.collect_orphan_generations()
            # Capacity includes valid candidates recovered from a previous process.
            # Counting only _pending would allow unlimited Push requests after restart.
            if (
                sum(
                    1
                    for path in self._candidates.iterdir()
                    if path.is_dir()
                    and not path.is_symlink()
                    and len(path.name) == 32
                    and all(char in "0123456789abcdef" for char in path.name)
                )
                >= self._capacity
            ):
                raise SnapshotError("candidate store is full")
            if (self.active_revision() or "") != expected_revision:
                raise SnapshotConflictError("active revision differs from expected revision")
            canonical, digest = self._canonical(
                files, max_files=self._max_files, max_bytes=self._max_bytes
            )
            if self.active_revision() is not None:
                previous = self.read_generation()
                current = dict(canonical)
                for name, content in previous.items():
                    if (
                        name.startswith(_PUBLISHED_TEMPLATE_PREFIXES)
                        and current.get(name) != content
                    ):
                        raise SnapshotError("published template version cannot change or disappear")
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix=".validate-", dir=self._root) as directory:
                staging = Path(directory)
                self._write_generation(staging, canonical)
                validate(self._paths(staging))
            candidate = Candidate(
                candidate_id=uuid.uuid4().hex,
                base_revision=expected_revision,
                content_hash=digest,
                files=canonical,
                expires_at=time.monotonic() + self._ttl,
            )
            self._persist_candidate(candidate)
            self._pending[candidate.candidate_id] = candidate
            return candidate

    def get(self, candidate_id: str) -> Candidate:
        with self._storage_lock:
            self._prune()
            candidate = self._pending.get(candidate_id)
            if candidate is None:
                candidate = self._load_candidate(candidate_id)
                self._pending[candidate_id] = candidate
            return candidate

    def _receipt_path(self, candidate_id: str) -> Path:
        digest = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
        return self._root / "receipts" / f"{digest}.json"

    def _write_receipt(
        self,
        candidate_id: str,
        base_revision: str,
        revision: str,
        *,
        state: str,
    ) -> None:
        directory = self._root / "receipts"
        directory.mkdir(parents=True, exist_ok=True)
        destination = self._receipt_path(candidate_id)
        payload = {
            "candidate_id": candidate_id,
            "base_revision": base_revision,
            "revision": revision,
            "state": state,
        }
        temporary = directory / f".receipt-{uuid.uuid4().hex}"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            self._sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def committed_revision(self, candidate_id: str, expected_revision: str) -> str | None:
        """Identify an earlier file commit, including crash-after-pointer cases."""
        receipt_path = self._receipt_path(candidate_id)
        if not receipt_path.is_file():
            return None
        try:
            record = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotError("corrupt configuration commit receipt") from exc
        if record.get("candidate_id") != candidate_id:
            raise SnapshotError("configuration commit receipt identity mismatch")
        if record.get("base_revision") != expected_revision:
            raise SnapshotConflictError("candidate already committed with another base")
        revision = record.get("revision")
        if not isinstance(revision, str) or len(revision) != 64:
            raise SnapshotError("corrupt configuration commit revision")
        state = record.get("state")
        if state == "COMMITTED":
            return revision
        if state == "PREPARED" and self.active_revision() == revision:
            self._write_receipt(candidate_id, expected_revision, revision, state="COMMITTED")
            return revision
        if state == "PREPARED":
            return None
        raise SnapshotError("corrupt configuration commit state")

    async def commit(
        self,
        candidate_id: str,
        *,
        expected_revision: str,
        validate: Callable[[SnapshotPaths], None],
    ) -> CommitResult:
        """Commit without blocking the event loop on fsync or rename.

        Cancellation never abandons an in-progress disk transaction.
        """
        async with self._lock:
            operation = asyncio.create_task(
                asyncio.to_thread(self._commit_sync, candidate_id, expected_revision, validate)
            )
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Keep the lock through repeated cancellation until the
                # underlying synchronous transaction has actually settled.
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if operation.done() and not operation.cancelled():
                    operation.exception()
                raise

    def _commit_sync(
        self,
        candidate_id: str,
        expected_revision: str,
        validate: Callable[[SnapshotPaths], None],
    ) -> CommitResult:
        with self._storage_lock:
            prior = self.committed_revision(candidate_id, expected_revision)
            if prior is not None:
                return CommitResult(prior, self._paths(self._generations / prior))
            candidate = self.get(candidate_id)
            current = self.active_revision() or ""
            if current != expected_revision or current != candidate.base_revision:
                raise SnapshotConflictError("candidate base revision is stale")
            self._generations.mkdir(parents=True, exist_ok=True)
            target = self._generations / candidate.content_hash
            if not target.exists():
                staging = Path(tempfile.mkdtemp(prefix=".generation-", dir=self._generations))
                try:
                    self._write_generation(staging, candidate.files)
                    validate(self._paths(staging))
                    # Promotion is atomic on a single filesystem.
                    os.rename(staging, target)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            else:
                self.read_generation(candidate.content_hash)
                validate(self._paths(target))
            self._sync_directory(self._generations)
            self._write_receipt(
                candidate_id, expected_revision, candidate.content_hash, state="PREPARED"
            )
            temp_pointer = self._root / f".CURRENT-{uuid.uuid4().hex}"
            try:
                with temp_pointer.open("w", encoding="ascii") as stream:
                    stream.write(candidate.content_hash + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_pointer, self._pointer)
                self._sync_directory(self._root)
            finally:
                temp_pointer.unlink(missing_ok=True)
            self._write_receipt(
                candidate_id, expected_revision, candidate.content_hash, state="COMMITTED"
            )
            self._pending.pop(candidate_id, None)
            # The durable receipt, not the candidate, owns committed replay state.
            # Reclaim the candidate slot immediately after the commit is recorded.
            try:
                shutil.rmtree(self._candidate_path(candidate_id))
                self._sync_directory(self._candidates)
            except OSError:
                # Cleanup must not turn an already committed transaction into a failure.
                pass
            return CommitResult(candidate.content_hash, self._paths(target))

    @classmethod
    def _write_generation(cls, root: Path, files: tuple[tuple[str, bytes], ...]) -> None:
        directories = {root}
        for name, content in files:
            path = root.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.resolve().is_relative_to(root.resolve()):
                raise SnapshotError("snapshot path escapes generation directory")
            parent = path.parent
            while parent != root:
                directories.add(parent)
                parent = parent.parent
            with path.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        # Parent directory entries must be durable before generation promotion.
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            cls._sync_directory(directory)

    def read_generation(self, revision: str | None = None) -> dict[str, bytes]:
        """Read one immutable configuration revision, never a mixed directory."""
        selected = revision or self.active_revision()
        if (
            selected is None
            or len(selected) != 64
            or any(char not in "0123456789abcdef" for char in selected)
        ):
            raise SnapshotError("unknown configuration revision")
        root = self._generations / selected
        if not root.is_dir():
            raise SnapshotError("configuration revision is unavailable")
        files: dict[str, bytes] = {}
        for path in root.rglob("*.yaml"):
            if path.is_symlink() or not path.is_file():
                raise SnapshotError("invalid generation file")
            relative = path.relative_to(root).as_posix()
            self._normalize(relative)
            files[relative] = path.read_bytes()
        canonical, digest = self._canonical(
            files, max_files=self._max_files, max_bytes=self._max_bytes
        )
        if digest != selected:
            raise SnapshotError("configuration generation checksum mismatch")
        return dict(canonical)

    def collect_source(self, paths: SnapshotPaths) -> dict[str, bytes]:
        """Collect only Collector-owned files from existing config roots."""
        roots = {
            "services/collector.yaml": paths.service,
            "instances/site.yaml": paths.site,
            "instances/devices.yaml": paths.devices,
            "templates/units_standard.yaml": paths.templates / "units_standard.yaml",
            "templates/business_points_standard.yaml": paths.templates / "business_points_standard.yaml",
        }
        for category in ("protocols", "device_models", "point_tables", "tasks"):
            directory = paths.templates / category
            if directory.is_dir():
                for template_file in directory.rglob("*.yaml"):
                    if template_file.is_symlink() or not template_file.resolve().is_relative_to(
                        paths.templates
                    ):
                        raise SnapshotError("unsafe source template file")
                    relative = template_file.relative_to(paths.templates).as_posix()
                    roots[f"templates/{relative}"] = template_file
        result: dict[str, bytes] = {}
        for relative, file_path in roots.items():
            self._normalize(relative)
            if file_path is None or file_path.is_symlink() or not file_path.is_file():
                raise SnapshotError(f"missing or unsafe source file: {relative}")
            result[relative] = file_path.read_bytes()
        self._canonical(result, max_files=self._max_files, max_bytes=self._max_bytes)
        return result

    @staticmethod
    def _sync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
