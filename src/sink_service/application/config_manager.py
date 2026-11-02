"""Writer-owned Get/Push/Apply coordination for immutable configuration snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import yaml

from sink_service.application.config import validate_snapshot
from sink_service.domain.config import ConfigurationError, SnapshotFile, ValidatedSnapshot
from sink_service.infrastructure.config_repository import GenerationStore, snapshot_from_wire


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


class ConfigManager:
    """Serialize all writer mutations; runtime application follows disk commit."""

    def __init__(
        self,
        store: GenerationStore,
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
        self._reconcile = reconcile

    @property
    def disk(self) -> ValidatedSnapshot:
        return self._disk

    @property
    def runtime(self) -> ValidatedSnapshot:
        return self._runtime

    def candidate(self, candidate_id: str) -> ValidatedSnapshot:
        candidate = self._candidates.get(candidate_id)
        if candidate is None or candidate.expires_at <= datetime.now(UTC):
            raise ConfigurationError("candidate missing or expired")
        return candidate.snapshot

    def _require_writer(self) -> None:
        if not self._writer:
            raise ConfigurationError("configuration changes require the designated Writer")

    async def push(
        self,
        files: list[tuple[str, bytes, int, str]],
        expected_revision: str,
    ) -> tuple[str, ValidatedSnapshot]:
        self._require_writer()
        async with self._lock:
            if expected_revision != self._disk.revision:
                raise ConfigurationError("configuration revision conflict")
            snapshot = snapshot_from_wire(files)
            now = datetime.now(UTC)
            self._candidates = {
                key: value
                for key, value in self._candidates.items()
                if value.expires_at > now
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
                raise ConfigurationError("candidate missing or expired")
            if (
                candidate.base_revision != expected_revision
                or self._disk.revision != expected_revision
            ):
                raise ConfigurationError("configuration revision conflict")
            snapshot = candidate.snapshot
            committed = self._store.commit(snapshot, expected_revision, old=self._disk)
            previous_service = self._runtime.service
            self._disk = snapshot
            restart_required = previous_service != snapshot.service
            previous_deliveries = self._runtime.deliveries
            changed = tuple(
                sorted(
                    key
                    for key in previous_deliveries.keys() | snapshot.deliveries.keys()
                    if previous_deliveries.get(key) != snapshot.deliveries.get(key)
                )
            )
            if self._reconcile is not None and not restart_required:
                try:
                    changed = await self._reconcile(snapshot)
                    self._runtime = snapshot
                except Exception:
                    # Disk is already committed; do not advertise runtime success.
                    pass
            application_id = secrets.token_urlsafe(24)
            application = Application(
                application_id=application_id,
                file_commit=committed,
                effective_revision=self._runtime.revision,
                restart_required=restart_required,
                pending=changed,
            )
            self._applications[application_id] = application
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
                raise ConfigurationError("configuration revision conflict")
            if delivery_id not in self._disk.deliveries:
                raise KeyError(f"unknown Delivery: {delivery_id}")
            updated: list[SnapshotFile] = []
            for item in self._disk.files:
                if item.path != "instances/deliveries.yaml":
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
                delivery["desired_state"] = "START" if running else "STOP"
                updated.append(
                    SnapshotFile(
                        item.path,
                        yaml.safe_dump(
                            doc, allow_unicode=True, sort_keys=False
                        ).encode("utf-8"),
                    )
                )
            snapshot = validate_snapshot(tuple(updated))
            committed = self._store.commit(
                snapshot, expected_revision, old=self._disk
            )
            self._disk = snapshot
            if self._reconcile is not None:
                await self._reconcile(snapshot)
                self._runtime = snapshot
            return snapshot, committed

    async def mark_active(self, snapshot: ValidatedSnapshot) -> None:
        """Reader/Delivery reconciler reports actual successful convergence."""
        async with self._lock:
            if snapshot.revision != self._disk.revision:
                raise ConfigurationError("stale runtime revision")
            self._runtime = snapshot
