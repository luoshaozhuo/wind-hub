"""Writer-owned Get/Push/Apply coordination for immutable configuration snapshots."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import yaml

from sink_service.application.config import snapshot_from_wire, validate_snapshot
from sink_service.application.delivery_definition import delivery_definitions
from sink_service.domain.config import (
    CandidateNotFoundError,
    ConfigurationError,
    InvalidConfigurationError,
    RevisionConflictError,
    SnapshotFile,
    ValidatedSnapshot,
    WriterRequiredError,
    config_sources,
)


class SnapshotCommitPort(Protocol):
    """Atomic persistent commit port implemented by infrastructure."""

    def commit(
        self,
        snapshot: ValidatedSnapshot,
        expected_revision: str,
        *,
        old: ValidatedSnapshot | None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    snapshot: ValidatedSnapshot
    base_revision: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Application:
    application_id: str
    file_commit: str
    effective_revision: str
    restart_required: bool
    pending: tuple[str, ...]
    applied: tuple[str, ...]
    error: str


class ConfigManager:
    """Serialize all writer mutations; runtime application follows disk commit."""

    def __init__(
        self,
        store: SnapshotCommitPort,
        initial: ValidatedSnapshot,
        *,
        writer: bool,
        candidate_ttl_seconds: int = 300,
        max_candidates: int = 8,
        reconcile: Callable[[ValidatedSnapshot], Awaitable[tuple[str, ...]]] | None = None,
    ) -> None:
        self._store = store
        self._disk = initial
        self._runtime = initial
        self._writer = writer
        self._candidate_ttl = timedelta(seconds=candidate_ttl_seconds)
        self._max_candidates = max_candidates
        self._lock = asyncio.Lock()
        self._candidates: dict[str, Candidate] = {}
        self._applications: dict[str, Application] = {}
        self._latest_application: dict[str, Application] = {}
        self._reconcile = reconcile
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    def record_reader_sync_error(self, error: str) -> None:
        """Expose Reader polling failures through existing health/diagnose RPCs."""
        if not self._writer:
            self._last_error = error

    @property
    def disk(self) -> ValidatedSnapshot:
        return self._disk

    @property
    def runtime(self) -> ValidatedSnapshot:
        return self._runtime

    def candidate(self, candidate_id: str) -> ValidatedSnapshot:
        candidate = self._candidates.get(candidate_id)
        if candidate is None or candidate.expires_at <= datetime.now(UTC):
            raise CandidateNotFoundError("candidate missing or expired")
        return candidate.snapshot

    @property
    def latest_application(self) -> Application | None:
        """Most recent Apply outcome for the current disk revision, if any."""
        return self._latest_application.get(self._disk.revision)

    def _require_writer(self) -> None:
        if not self._writer:
            raise WriterRequiredError("configuration changes require the designated Writer")

    async def push(
        self,
        files: list[tuple[str, bytes, int, str]],
        expected_revision: str,
    ) -> tuple[str, ValidatedSnapshot]:
        self._require_writer()
        async with self._lock:
            if expected_revision != self._disk.revision:
                raise RevisionConflictError("configuration revision conflict")
            try:
                snapshot = snapshot_from_wire(files)
            except ConfigurationError as exc:
                raise InvalidConfigurationError(str(exc)) from exc
            # Historical Durable ownership is not transferable by merely
            # editing the current filter. No old backlog may be ACKed under
            # a new source responsibility without a controlled migration.
            previous_reliable = {
                item.delivery_id for item in delivery_definitions(self._disk)
                if item.channel == 'reliable'
            }
            next_reliable = {
                item.delivery_id for item in delivery_definitions(snapshot)
                if item.channel == 'reliable'
            }
            for identifier in previous_reliable & next_reliable:
                old_source = self._disk.deliveries[identifier].get('source')
                new_source = snapshot.deliveries[identifier].get('source')
                if old_source != new_source:
                    raise InvalidConfigurationError(
                        f'deliveries.{identifier}.source: Reliable source change '
                        'requires a separately audited Durable migration'
                    )
            now = datetime.now(UTC)
            self._candidates = {
                key: value for key, value in self._candidates.items() if value.expires_at > now
            }
            if len(self._candidates) >= self._max_candidates:
                raise ConfigurationError("candidate capacity exceeded")
            identifier = secrets.token_urlsafe(24)
            self._candidates[identifier] = Candidate(
                snapshot=snapshot,
                base_revision=expected_revision,
                expires_at=now + self._candidate_ttl,
            )
            return identifier, snapshot

    async def apply(self, candidate_id: str, expected_revision: str) -> Application:
        self._require_writer()
        async with self._lock:
            candidate = self._candidates.get(candidate_id)
            if candidate is None or candidate.expires_at <= datetime.now(UTC):
                raise CandidateNotFoundError("candidate missing or expired")
            if (
                candidate.base_revision != expected_revision
                or self._disk.revision != expected_revision
            ):
                raise RevisionConflictError("configuration revision conflict")
            snapshot = candidate.snapshot
            committed = self._store.commit(snapshot, expected_revision, old=self._disk)
            previous_services = {
                item.path: item.content
                for item in self._runtime.files
                if item.path.startswith("configs/services/")
            }
            current_services = {
                item.path: item.content
                for item in snapshot.files
                if item.path.startswith("configs/services/")
            }
            self._disk = snapshot
            restart_required = previous_services != current_services
            previous_deliveries = self._runtime.deliveries
            # Include indirect Sink edits: a Delivery can need rebinding even
            # when its own Instance YAML is byte-for-byte unchanged.
            affected: set[str] = set()
            for identifier in previous_deliveries.keys() | snapshot.deliveries.keys():
                before_delivery = previous_deliveries.get(identifier)
                after_delivery = snapshot.deliveries.get(identifier)
                if before_delivery != after_delivery:
                    affected.add(identifier)
                    continue
                if before_delivery is None or after_delivery is None:
                    continue
                sink_id = before_delivery['sink_id']
                if self._runtime.sinks.get(sink_id) != snapshot.sinks.get(sink_id):
                    affected.add(identifier)
            changed = tuple(sorted(affected))
            all_ids = {
                item.delivery_id
                for source in (self._runtime, snapshot)
                for item in delivery_definitions(source)
            }
            applied: tuple[str, ...] = ()
            error = ""
            # A Service-level source or runtime change is restart-only.
            # Never reconcile against a new config_sources graph in the old process.
            if self._reconcile is not None and not restart_required:
                try:
                    applied = await self._reconcile(snapshot)
                    # This revision is effective only after every affected
                    # local object has converged.
                    if all_ids.issubset(set(applied)):
                        self._runtime = snapshot
                        self._last_error = ""
                    else:
                        error = "one or more Deliveries are not converged"
                        self._last_error = error
                except Exception as exc:
                    # Disk is already committed; report actual pending convergence.
                    error = str(exc)
                    self._last_error = error
            # An unchanged local Delivery may still be unconverged. Include
            # every such object in the Apply result, not only file-diff IDs.
            pending = tuple(sorted((set(changed) | all_ids) - set(applied)))
            application_id = secrets.token_urlsafe(24)
            application = Application(
                application_id=application_id,
                file_commit=committed,
                effective_revision=self._runtime.revision,
                restart_required=restart_required,
                pending=pending,
                applied=applied,
                error=error,
            )
            self._applications[application_id] = application
            self._latest_application[snapshot.revision] = application
            del self._candidates[candidate_id]
            return application

    async def set_delivery_desired(
        self,
        delivery_id: str,
        *,
        running: bool,
        expected_revision: str,
    ) -> tuple[ValidatedSnapshot, str]:
        """Persist operator intent via the same CAS as full snapshot Apply."""
        self._require_writer()
        async with self._lock:
            if self._disk.revision != expected_revision:
                raise RevisionConflictError("configuration revision conflict")
            if delivery_id not in self._disk.deliveries:
                raise KeyError(f"unknown Delivery: {delivery_id}")
            runtime_services = {
                item.path: item.content
                for item in self._runtime.files
                if item.path.startswith("configs/services/")
            }
            committed_services = {
                item.path: item.content
                for item in self._disk.files
                if item.path.startswith("configs/services/")
            }
            if runtime_services != committed_services:
                raise ConfigurationError(
                    "Service configuration changed; restart is required "
                    "before further Delivery operations"
                )
            updated: list[SnapshotFile] = []
            source_paths, _ = config_sources(self._disk.service)
            found = False
            for item in self._disk.files:
                if item.path != source_paths["deliveries"]:
                    updated.append(item)
                    continue
                doc = yaml.safe_load(item.content)
                if not isinstance(doc, dict):
                    raise ConfigurationError("invalid Delivery instances file")
                deliveries = doc.get("deliveries")
                if not isinstance(deliveries, dict):
                    raise ConfigurationError("invalid Deliveries mapping")
                delivery = deliveries.get(delivery_id)
                if not isinstance(delivery, dict):
                    raise ConfigurationError("Delivery is missing in current snapshot")
                found = True
                delivery["desired_state"] = "START" if running else "STOP"
                doc["revision"] = secrets.token_hex(16)
                updated.append(
                    SnapshotFile(
                        item.path,
                        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8"),
                    )
                )
            if not found:
                raise ConfigurationError("Delivery instance pathname missing from snapshot")
            try:
                snapshot = validate_snapshot(tuple(updated))
            except ConfigurationError as exc:
                raise InvalidConfigurationError(str(exc)) from exc
            committed = self._store.commit(snapshot, expected_revision, old=self._disk)
            self._disk = snapshot
            if self._reconcile is not None:
                try:
                    applied = await self._reconcile(snapshot)
                    local_ids = {
                        item.delivery_id
                        for source in (self._runtime, snapshot)
                        for item in delivery_definitions(source)
                    }
                    if local_ids.issubset(set(applied)):
                        self._runtime = snapshot
                        self._last_error = ""
                    else:
                        self._last_error = (
                            "one or more local Deliveries are not converged"
                        )
                except Exception as exc:
                    # The desired state has already been committed to disk.
                    self._last_error = str(exc)
            return snapshot, committed
