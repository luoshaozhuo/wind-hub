"""Collector configuration repository and runtime ports."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from collector.application.config_models import Candidate, CommitResult, SnapshotPaths


class SnapshotRepositoryPort(Protocol):
    def active_revision(self) -> str | None: ...
    def active_paths(self) -> SnapshotPaths: ...
    def read_generation(self, revision: str | None = None) -> dict[str, bytes]: ...
    def get(self, candidate_id: str) -> Candidate: ...
    def committed_revision(self, candidate_id: str, expected_revision: str) -> str | None: ...
    def stage(
        self,
        files: dict[str, bytes],
        *,
        expected_revision: str,
        validate: Callable[[SnapshotPaths], None],
    ) -> Candidate: ...
    async def commit(
        self,
        candidate_id: str,
        *,
        expected_revision: str,
        validate: Callable[[SnapshotPaths], None],
    ) -> CommitResult: ...


class RuntimeConfigurationPort(Protocol):
    async def converge(self, paths: SnapshotPaths, revision: str) -> dict[str, str]:
        """Apply changed Collector runtime objects; return failures by object ID."""
        ...
