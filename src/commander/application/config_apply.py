"""Application coordination of Commander configuration apply transactions."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from commander.application.service_lifecycle import CommanderService


@dataclass(frozen=True, slots=True)
class SnapshotCandidate:
    base_revision: str
    files: dict[str, bytes]
    revision: str


@dataclass(frozen=True, slots=True)
class SnapshotInput:
    relative_path: str
    content: bytes
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PushOutcome:
    candidate_id: str
    valid: bool
    base_revision: str
    validation_message: str = ""


@dataclass(frozen=True, slots=True)
class ApplyObjectResult:
    object_id: str
    status: str
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    file_commit: str
    effective_revision: str
    restart_required: bool
    application_id: str
    apply_results: tuple[ApplyObjectResult, ...]


class SnapshotApplyPort(Protocol):
    """Durable storage operations; the application owns the transaction decision."""

    def collect_files(self) -> dict[str, bytes]: ...

    def prepare_candidate(
        self, current: dict[str, bytes], files: tuple[SnapshotInput, ...]
    ) -> dict[str, bytes]: ...

    async def get_candidate_validation_error(self, files: dict[str, bytes]) -> str | None: ...

    async def validate_files(self, files: dict[str, bytes]) -> None: ...

    async def commit_files(self, changed: dict[str, bytes]) -> None: ...


def snapshot_revision(files: dict[str, bytes]) -> str:
    """Canonical digest shared by optimistic concurrency checks."""
    digest = hashlib.sha256()
    for key, content in sorted(files.items()):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


class CommanderConfigApply:
    """Single-writer candidate apply, durable commit and runtime convergence."""

    def __init__(
        self,
        service: CommanderService,
        repository: SnapshotApplyPort,
        lock: asyncio.Lock,
    ) -> None:
        self._service = service
        self._repository = repository
        self._lock = lock
        self._candidates: dict[str, SnapshotCandidate] = {}

    def get_candidate(self, candidate_id: str) -> SnapshotCandidate | None:
        """Return the currently accepted candidate under the caller's lock."""
        return self._candidates.get(candidate_id)

    async def push(
        self, files: tuple[SnapshotInput, ...], expected_revision: str
    ) -> PushOutcome:
        async with self._lock:
            current = await asyncio.to_thread(self._repository.collect_files)
            revision = snapshot_revision(current)
            if expected_revision != revision:
                raise RuntimeError("expected_revision does not match disk revision")
            candidate = self._repository.prepare_candidate(current, files)
            error = await self._repository.get_candidate_validation_error(candidate)
            if error is not None:
                return PushOutcome(
                    candidate_id="",
                    valid=False,
                    base_revision=revision,
                    validation_message=error,
                )
            candidate_id = str(uuid4())
            self._candidates.clear()
            self._candidates[candidate_id] = SnapshotCandidate(
                revision, candidate, snapshot_revision(candidate)
            )
            return PushOutcome(
                candidate_id=candidate_id,
                valid=True,
                base_revision=revision,
            )

    async def apply(self, candidate_id: str, expected_revision: str) -> ApplyOutcome:
        async with self._lock:
            operation = asyncio.create_task(
                self._apply_locked(candidate_id, expected_revision)
            )
            try:
                result = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # The lock is not released until native file work/reload finishes.
                await operation
                raise
            except Exception:
                self._service.metrics.config_failed()
                raise
            if any(
                item.status in {"FAILED", "BLOCKED"} for item in result.apply_results
            ):
                self._service.metrics.config_failed()
            return result

    async def _apply_locked(
        self, candidate_id: str, expected_revision: str
    ) -> ApplyOutcome:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        current = await asyncio.to_thread(self._repository.collect_files)
        revision = snapshot_revision(current)
        if (
            expected_revision != candidate.base_revision
            or revision != candidate.base_revision
        ):
            raise RuntimeError("configuration revision conflict")
        await self._repository.validate_files(candidate.files)
        changed = {
            name: content
            for name, content in candidate.files.items()
            if name not in current or current[name] != content
        }
        # Durable commit must finish before reload; a reload failure is partial success.
        await self._repository.commit_files(changed)
        self._candidates.pop(candidate_id, None)
        try:
            report = await self._service.reload()
        except Exception as exc:
            return ApplyOutcome(
                file_commit=snapshot_revision(await asyncio.to_thread(self._repository.collect_files)),
                effective_revision=self._service.config_manager.revision,
                restart_required=True,
                application_id=str(uuid4()),
                apply_results=(
                    ApplyObjectResult(
                        object_id="commander",
                        status="FAILED",
                        error_code="RELOAD_FAILED",
                        message=str(exc),
                    ),
                ),
            )
        return ApplyOutcome(
            file_commit=snapshot_revision(await asyncio.to_thread(self._repository.collect_files)),
            effective_revision=report.revision,
            restart_required=report.restart_required,
            application_id=str(uuid4()),
            apply_results=tuple(
                ApplyObjectResult(
                    object_id=device,
                    status="BLOCKED" if device in report.blocked else "APPLIED",
                )
                for device in (*report.changed, *report.blocked)
            ),
        )
