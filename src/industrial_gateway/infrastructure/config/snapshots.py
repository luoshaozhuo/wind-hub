"""Manage Gateway-owned Service snapshots without modifying shared sources."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import yaml

from configuration.versioned_templates import config_hash
from core.application.errors import ConfigError
from core.infrastructure.config import parse_yaml
from industrial_gateway.infrastructure.config.v1_resolution import (
    ResolvedGateway,
    load_gateway_v1,
)
from industrial_gateway.infrastructure.config.v1_schema import service_path
from industrial_gateway.infrastructure.runtime import ApplicationResult, GatewayRuntime

_MAX_FILE_SIZE = 2097152


@dataclass(frozen=True, slots=True)
class Candidate:
    content: bytes
    resolved: ResolvedGateway
    base_revision: str


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    relative_path: str
    content: bytes
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    path: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SnapshotView:
    snapshot_id: str
    revision: str
    files: tuple[SnapshotFile, ...]
    application_id: str
    apply_results: tuple[ApplicationResult, ...]
    last_error: str


@dataclass(frozen=True, slots=True)
class PushResult:
    valid: bool
    base_revision: str
    candidate_id: str = ""
    validation: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    file_commit: str
    apply_results: tuple[ApplicationResult, ...]
    effective_revision: str
    restart_required: bool
    application_id: str


class ConfigSnapshots:
    """Stage and apply only the explicitly owned Service file."""

    def __init__(
        self,
        runtime: GatewayRuntime,
        service_file: Path,
        project_root: Path,
    ) -> None:
        self.runtime = runtime
        self.root = project_root.resolve(strict=True)
        self.path = service_path(self.root, service_file)
        self.relative_path = self.path.relative_to(self.root).as_posix()
        self.candidates: dict[str, Candidate] = {}
        self.lock = asyncio.Lock()
        self.runtime_content = self.path.read_bytes()
        self.runtime_revision = runtime.config.revision
        self._process_service = runtime.config.service
        self.last_error = ""
        self.last_application = ""
        self.last_results: list[ApplicationResult] = []

    def disk(self) -> tuple[ResolvedGateway, bytes]:
        content = self.path.read_bytes()
        document = parse_yaml(content.decode("utf-8"))
        config = load_gateway_v1(
            self.path,
            project_root=self.root,
            candidate_document=document,
        )
        return config, content

    def snapshot_file(self, content: bytes) -> SnapshotFile:
        return SnapshotFile(
            relative_path=self.relative_path,
            content=content,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    async def get(self, scope: int, candidate_id: str) -> SnapshotView:
        """Return a consistent Service file for the selected scope."""
        async with self.lock:
            return self._get_locked(scope, candidate_id)

    def _get_locked(self, scope: int, candidate_id: str) -> SnapshotView:
        if scope in {0, 1}:
            config, content = self.disk()
            revision = config.revision
        elif scope == 2:
            content = self.runtime_content
            revision = self.runtime_revision
        elif scope == 3:
            candidate = self.candidates[candidate_id]
            content = candidate.content
            revision = candidate.resolved.revision
        else:
            raise ValueError("unknown snapshot scope")
        return SnapshotView(
            snapshot_id=candidate_id or uuid4().hex,
            revision=revision,
            files=(self.snapshot_file(content),),
            application_id=self.last_application,
            apply_results=tuple(self.last_results),
            last_error=self.last_error,
        )

    async def push(self, files: list[SnapshotFile], expected_revision: str) -> PushResult:
        """Validate and stage without changing disk or running services."""
        async with self.lock:
            disk, _ = self.disk()
            issues: list[ValidationIssue] = []
            if not expected_revision or expected_revision != disk.revision:
                issues.append(
                    ValidationIssue(
                        path=self.relative_path,
                        code="REVISION_CONFLICT",
                        message="expected revision differs from disk",
                    )
                )
            if len(files) != 1 or files[0].relative_path != self.relative_path:
                issues.append(
                    ValidationIssue(
                        path=self.relative_path,
                        code="FILE_SET",
                        message="only the Gateway Service file can be replaced",
                    )
                )
            if issues:
                return PushResult(
                    valid=False, base_revision=disk.revision, validation=tuple(issues)
                )
            item = files[0]
            content = bytes(item.content)
            if (
                not content
                or len(content) > _MAX_FILE_SIZE
                or item.size_bytes != len(content)
                or item.sha256 != hashlib.sha256(content).hexdigest()
            ):
                return PushResult(
                    valid=False,
                    base_revision=disk.revision,
                    validation=(
                        ValidationIssue(
                            path=self.relative_path,
                            code="CONTENT_INTEGRITY",
                            message="size or digest mismatch",
                        ),
                    ),
                )
            try:
                candidate_doc = parse_yaml(content.decode("utf-8"))
                resolved = load_gateway_v1(
                    self.path,
                    project_root=self.root,
                    candidate_document=candidate_doc,
                )
            except (ConfigError, ValueError, OSError) as exc:
                return PushResult(
                    valid=False,
                    base_revision=disk.revision,
                    validation=(
                        ValidationIssue(
                            path=self.relative_path,
                            code="INVALID_CONFIG",
                            message=str(exc),
                        ),
                    ),
                )
            if len(self.candidates) >= 16:
                self.candidates.pop(next(iter(self.candidates)))
            candidate_id = uuid4().hex
            self.candidates[candidate_id] = Candidate(content, resolved, disk.revision)
            return PushResult(
                candidate_id=candidate_id,
                valid=True,
                base_revision=disk.revision,
            )

    def _commit_bytes(self, content: bytes) -> None:
        """Replace the Service file atomically, preserving the staged bytes."""
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _effective_snapshot(
        self,
        config: ResolvedGateway,
        *,
        restart_required: bool,
    ) -> tuple[bytes, str]:
        if not restart_required:
            return self.path.read_bytes(), config.revision
        old = parse_yaml(self.runtime_content.decode("utf-8"))
        candidate = parse_yaml(self.path.read_bytes().decode("utf-8"))
        old["servers"] = candidate["servers"]
        old["config_sources"] = candidate["config_sources"]
        content = yaml.safe_dump(old, allow_unicode=True, sort_keys=False).encode("utf-8")
        revision = config_hash({"service": old, "shared": config.shared_revision})
        return content, revision

    async def apply(
        self,
        candidate_id: str,
        expected_revision: str,
        *,
        operator: str,
    ) -> ApplyResult:
        """Commit the file and independently record per-Server apply outcomes."""
        async with self.lock:
            candidate = self.candidates[candidate_id]
            disk, _ = self.disk()
            if (
                not expected_revision
                or expected_revision != disk.revision
                or candidate.base_revision != disk.revision
            ):
                raise ValueError("stale candidate revision")
            document = parse_yaml(candidate.content.decode("utf-8"))
            resolved = load_gateway_v1(
                self.path,
                project_root=self.root,
                candidate_document=document,
            )
            self._commit_bytes(candidate.content)
            self.candidates.clear()
            application_id = uuid4().hex
            try:
                results, restart_required = await self.runtime.reconfigure(resolved)
                restart_required = restart_required or any(
                    (
                        self._process_service.grpc != resolved.service.grpc,
                        self._process_service.metrics != resolved.service.metrics,
                    )
                )
            except Exception as exc:
                results = [
                    ApplicationResult(
                        object_id="runtime",
                        status="FAILED",
                        error_code="APPLY_FAILED",
                        message=str(exc),
                    )
                ]
                restart_required = True
                self.last_error = str(exc)
            else:
                failures = [item for item in results if item.status == "FAILED"]
                if failures:
                    self.last_error = "partial runtime apply; inspect apply_results"
                    restart_required = True
                else:
                    self.last_error = ""
            if restart_required:
                results.append(
                    ApplicationResult(
                        object_id="service",
                        status="RESTART_REQUIRED",
                        message="service-level parameters become effective on restart",
                    )
                )
            details = ",".join(f"{item.object_id}:{item.status}" for item in results)
            self.runtime.observability.event(
                "config",
                (
                    f"Apply {application_id}: operator={operator}; "
                    f"candidate={candidate_id}; old={disk.revision}; "
                    f"new={resolved.revision}; "
                    f"file_commit={hashlib.sha256(candidate.content).hexdigest()}; "
                    f"results={details}"
                ),
                severity="WARNING" if self.last_error else "INFO",
                error_code="APPLY_FAILED" if self.last_error else "",
            )
            self.last_results = results
            self.last_application = application_id
            if not any(item.status == "FAILED" for item in results):
                content, revision = self._effective_snapshot(
                    resolved, restart_required=restart_required
                )
                self.runtime_content = content
                self.runtime_revision = revision
                effective_service = self.runtime.config.service
                if restart_required:
                    effective_service = effective_service.model_copy(
                        update={
                            "grpc": self._process_service.grpc,
                            "metrics": self._process_service.metrics,
                        }
                    )
                self.runtime.config = replace(
                    self.runtime.config,
                    service=effective_service,
                    revision=revision,
                )
            return ApplyResult(
                file_commit=hashlib.sha256(self.path.read_bytes()).hexdigest(),
                apply_results=tuple(results),
                effective_revision=self.runtime_revision,
                restart_required=restart_required,
                application_id=application_id,
            )
