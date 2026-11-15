"""Data Generator snapshot transaction coordination independent of gRPC."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from data_generator.application.runtime import Runtime
from data_generator.domain.scenario import Settings


@dataclass(frozen=True, slots=True)
class ConfigFile:
    path: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    files: tuple[ConfigFile, ...]
    revision: str
    snapshot_id: str
    old_bindings: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class ConfigApplyResult:
    disk_revision: str
    effective_revision: str
    restart_required: bool
    scenarios: tuple[tuple[str, str], ...]


class SnapshotStorePort(Protocol):
    settings: Settings

    def get(self, scope: str, candidate_id: str = "") -> tuple[ConfigFile, ...]: ...
    def disk_revision(self) -> str: ...
    def candidate_revision(self, candidate_id: str) -> str: ...
    def push(self, files: tuple[ConfigFile, ...], expected: str) -> str: ...
    def apply(self, candidate_id: str, expected: str) -> tuple[Settings, bool]: ...


class ConfigurationCoordinator:
    """Serialize all snapshot reads, candidate staging and durable Apply."""

    def __init__(self, runtime: Runtime, store: SnapshotStorePort) -> None:
        self._runtime = runtime
        self._store = store
        self._lock = asyncio.Lock()

    @property
    def disk_revision(self) -> str:
        return self._store.settings.revision

    async def get(self, scope: str, candidate_id: str) -> ConfigSnapshot:
        async with self._lock:
            files = await asyncio.to_thread(self._store.get, scope, candidate_id)
            if scope == "RUNTIME":
                revision = self._runtime.settings.revision
            elif scope == "CANDIDATE":
                revision = self._store.candidate_revision(candidate_id)
            else:
                revision = await asyncio.to_thread(self._store.disk_revision)
            old_bindings = tuple(
                (
                    state.generation_id,
                    state.scenario.scenario_id,
                    state.scenario.version,
                )
                for state in self._runtime.generations.values()
                if scope == "RUNTIME"
                and state.actual_state in ("STARTING", "RUNNING", "STOPPING")
                and state.scenario
                != self._runtime.settings.scenarios.get(state.scenario.scenario_id)
            )
            return ConfigSnapshot(
                files,
                revision,
                candidate_id if scope == "CANDIDATE" else revision,
                old_bindings,
            )

    async def push(self, files: tuple[ConfigFile, ...], expected: str) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._store.push, files, expected)

    async def apply(self, candidate_id: str, expected: str) -> ConfigApplyResult:
        async with self._lock:
            try:
                settings, restart = await self._runtime.commit_configuration(
                    lambda: self._store.apply(candidate_id, expected)
                )
            except Exception as exc:
                self._runtime.record("ERROR", "config", "APPLY_FAILED", str(exc))
                raise
            self._runtime.record("INFO", "config", "APPLIED", settings.revision)
            active = {
                state.scenario.scenario_id
                for state in self._runtime.generations.values()
                if state.actual_state in ("STARTING", "RUNNING", "STOPPING")
            }
            states = tuple(
                (
                    name,
                    "RESTART_REQUIRED"
                    if restart
                    else "BOUND_OLD_VERSION"
                    if name in active
                    else "APPLIED",
                )
                for name in settings.scenarios
            )
            return ConfigApplyResult(
                disk_revision=settings.revision,
                effective_revision=self._runtime.settings.revision,
                restart_required=restart,
                scenarios=states,
            )
