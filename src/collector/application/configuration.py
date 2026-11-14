"""Collector configuration coordination across disk and active runtime.

The repository atomically selects a complete disk generation. Applying that
generation to live devices is a separate, observable operation. Core supplies
YAML/domain validation through the injected callback.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config_models import (
    Candidate,
    CommitResult,
    SnapshotConflictError,
    SnapshotPaths,
    snapshot_digest,
)
from .ports.configuration import RuntimeConfigurationPort, SnapshotRepositoryPort
from .telemetry import CollectorTelemetry


@dataclass(frozen=True, slots=True)
class ConfigurationStatus:
    disk_revision: str
    runtime_revision: str
    restart_required: bool
    apply_errors: dict[str, str]

    @property
    def converged(self) -> bool:
        return (
            not self.restart_required
            and not self.apply_errors
            and (self.disk_revision == self.runtime_revision)
        )


class CollectorConfigurationManager:
    """Own Get/Push/Apply transaction ordering for this Collector only."""

    def __init__(
        self,
        repository: SnapshotRepositoryPort,
        *,
        validate: Callable[[SnapshotPaths], None],
        runtime: RuntimeConfigurationPort,
        restart_required: Callable[[bytes, bytes], bool],
        telemetry: CollectorTelemetry | None = None,
    ) -> None:
        self._repository = repository
        self._validate = validate
        self._runtime = runtime
        self._restart_required = restart_required
        self._telemetry = telemetry
        self._apply_lock = asyncio.Lock()
        self._runtime_revision = ""
        self._runtime_files: dict[str, bytes] = {}
        self._runtime_service_path: Path | None = None
        self._restart_pending = False
        self._target_revision = ""
        self._errors: dict[str, str] = {}
        self._committed: OrderedDict[str, tuple[str, ConfigurationStatus]] = OrderedDict()

    def active_paths(self) -> SnapshotPaths:
        return self._repository.active_paths()

    def get_disk(self) -> tuple[str, dict[str, bytes]]:
        revision = self._repository.active_revision() or ""
        return revision, (self._repository.read_generation() if revision else {})

    def get_runtime(self) -> dict[str, bytes]:
        return dict(self._runtime_files)

    def candidate_revision(self, candidate_id: str) -> str:
        return self._repository.get(candidate_id).content_hash

    def get_candidate(self, candidate_id: str) -> dict[str, bytes]:
        candidate = self._repository.get(candidate_id)
        return dict(candidate.files)

    def push(self, files: dict[str, bytes], *, expected_revision: str) -> tuple[str, str]:
        candidate = self._repository.stage(
            files, expected_revision=expected_revision, validate=self._validate
        )
        return candidate.candidate_id, candidate.base_revision

    async def apply(self, candidate_id: str, *, expected_revision: str) -> ConfigurationStatus:
        async with self._apply_lock:
            previous = self._committed.get(candidate_id)
            if previous is not None:
                if previous[0] != expected_revision:
                    raise SnapshotConflictError("candidate already committed with another base")
                return previous[1]
            # Receipt promotion may fsync; never run disk I/O on the event loop.
            committed = await asyncio.to_thread(
                self._repository.committed_revision, candidate_id, expected_revision
            )
            if committed is not None:
                if self._repository.active_revision() == committed:
                    return self.status()
                return ConfigurationStatus(
                    disk_revision=committed,
                    runtime_revision=self._runtime_revision,
                    restart_required=True,
                    apply_errors={"collector": "original revision has been superseded"},
                )
            old_revision = self._repository.active_revision() or ""
            if old_revision != expected_revision:
                raise SnapshotConflictError("active configuration changed")
            result = await self._repository.commit(
                candidate_id,
                expected_revision=expected_revision,
                validate=self._validate,
            )
            files = await asyncio.to_thread(self._repository.read_generation, result.revision)
            current_service = self._runtime_files.get("services/collector.yaml")
            next_service = files["services/collector.yaml"]
            self._restart_pending = bool(
                current_service is not None
                and self._restart_required(current_service, next_service)
            )
            # Service-owned source references are startup-only. A new
            # Instance/Template tree must never leak into the live runtime
            # before its Service source selection takes effect on restart.
            source_changed = bool(
                current_service is not None
                and yaml.safe_load(current_service).get("config_sources")
                != yaml.safe_load(next_service).get("config_sources")
            )
            if source_changed:
                self._restart_pending = True
                self._target_revision = ""
                outcome = self.status()
                self._committed[candidate_id] = (expected_revision, outcome)
                if len(self._committed) > 256:
                    self._committed.popitem(last=False)
                return outcome
            if self._restart_pending and current_service is not None:
                files["services/collector.yaml"] = current_service
            target_revision = snapshot_digest(files)
            self._target_revision = target_revision
            effective_paths = SnapshotPaths(
                service=(
                    self._runtime_service_path
                    if self._restart_pending and self._runtime_service_path is not None
                    else result.paths.service
                ),
                instances=result.paths.instances,
                templates=result.paths.templates,
                site=result.paths.site,
                devices=result.paths.devices,
            )
            try:
                self._errors = await self._runtime.converge(effective_paths, target_revision)
            except Exception as exc:
                self._errors = {"collector": str(exc) or type(exc).__name__}
            if not self._errors and not self._restart_pending:
                self._runtime_files = files
                self._runtime_revision = target_revision
                self._runtime_service_path = effective_paths.service
            outcome = self.status()
            if self._telemetry is not None:
                self._telemetry.event(
                    "ERROR" if outcome.apply_errors else "INFO",
                    "config",
                    "APPLY_FAILED" if outcome.apply_errors else "APPLY_COMMITTED",
                    "; ".join(outcome.apply_errors.values()) or result.revision,
                )
            self._committed[candidate_id] = (expected_revision, outcome)
            if len(self._committed) > 256:
                self._committed.popitem(last=False)
            return outcome

    def _read_retry_material(self, revision: str) -> tuple[dict[str, bytes], SnapshotPaths]:
        """One thread hop for the full-generation read and its checksum verify."""
        return self._repository.read_generation(revision), self._repository.active_paths()

    async def retry_failed(self) -> ConfigurationStatus:
        """Converge the committed disk generation without repeating Apply."""
        async with self._apply_lock:
            if not self._errors:
                return self.status()
            revision = self._repository.active_revision()
            if revision is None:
                return self.status()
            files, active_paths = await asyncio.to_thread(self._read_retry_material, revision)
            if self._restart_pending and self._runtime_service_path is not None:
                files["services/collector.yaml"] = self._runtime_files["services/collector.yaml"]
            effective = SnapshotPaths(
                service=(
                    self._runtime_service_path
                    if self._restart_pending and self._runtime_service_path is not None
                    else active_paths.service
                ),
                instances=active_paths.instances,
                templates=active_paths.templates,
                site=active_paths.site,
                devices=active_paths.devices,
            )
            target_revision = snapshot_digest(files)
            self._target_revision = target_revision
            try:
                self._errors = await self._runtime.converge(effective, target_revision)
            except Exception as exc:
                self._errors = {"collector": str(exc) or type(exc).__name__}
            if not self._errors and not self._restart_pending:
                self._runtime_revision = target_revision
                self._runtime_files = files
                self._runtime_service_path = effective.service
            return self.status()

    @property
    def target_revision(self) -> str:
        """Current runtime application target, empty if pending source restart."""
        return self._target_revision

    def status(self) -> ConfigurationStatus:
        return ConfigurationStatus(
            disk_revision=self._repository.active_revision() or "",
            runtime_revision=self._runtime_revision,
            restart_required=self._restart_pending,
            apply_errors=dict(self._errors),
        )

    def mark_started(self, failures: dict[str, str] | None = None) -> None:
        """Report actual startup convergence instead of trusting process readiness."""
        errors = dict(failures or {})
        self._runtime_revision = self._repository.active_revision() or "" if not errors else ""
        # Even when some devices fail startup, the service configuration is
        # active and must remain the restart comparison baseline.
        active = self._repository.active_revision()
        self._runtime_files = self._repository.read_generation(active) if active else {}
        self._runtime_service_path = self._repository.active_paths().service if active else None
        self._restart_pending = False
        self._target_revision = self._runtime_revision
        self._errors = errors
