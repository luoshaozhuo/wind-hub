"""Bounded, immutable candidate configuration snapshots for Collector.

A pushed candidate is not an active configuration. The caller is responsible
for semantic validation and for committing a candidate to persistent storage.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath


class SnapshotCandidateError(ValueError):
    """A candidate violates the snapshot staging contract."""


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    relative_path: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class SnapshotCandidate:
    candidate_id: str
    base_revision: str
    digest: str
    files: tuple[SnapshotFile, ...]
    expires_at: float


class SnapshotCandidateStore:
    """Memory-only staging area with strict path, size, and lifetime bounds."""

    def __init__(
        self,
        *,
        max_candidates: int = 8,
        max_files: int = 512,
        max_total_bytes: int = 16 * 1024 * 1024,
        ttl_seconds: float = 300.0,
    ) -> None:
        if (
            max_candidates <= 0
            or max_files <= 0
            or max_total_bytes <= 0
            or ttl_seconds <= 0
        ):
            raise ValueError("candidate store limits must be positive")
        self._max_candidates = max_candidates
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        self._ttl_seconds = ttl_seconds
        self._candidates: dict[str, SnapshotCandidate] = {}

    @staticmethod
    def _path(raw: str) -> str:
        if (
            not raw
            or "\\" in raw
            or raw.startswith("/")
            or "\x00" in raw
            or any(part in ("", ".", "..") for part in raw.split("/"))
        ):
            raise SnapshotCandidateError("invalid snapshot relative path")
        path = PurePosixPath(raw)
        if not path.parts or path.as_posix() != raw:
            raise SnapshotCandidateError("snapshot path is not normalized")
        if path.parts[0] not in ("services", "instances", "templates"):
            raise SnapshotCandidateError("snapshot path is outside Collector config")
        if path.suffix != ".yaml":
            raise SnapshotCandidateError("snapshot requires YAML files")
        return raw

    def _expire(self, now: float) -> None:
        for identifier, candidate in tuple(self._candidates.items()):
            if candidate.expires_at <= now:
                del self._candidates[identifier]

    def push(
        self,
        files: tuple[tuple[str, bytes], ...],
        *,
        base_revision: str,
    ) -> SnapshotCandidate:
        """Stage an already semantically validated complete candidate."""
        now = time.monotonic()
        self._expire(now)
        if not base_revision:
            raise SnapshotCandidateError("base revision is required")
        if not files or len(files) > self._max_files:
            raise SnapshotCandidateError("snapshot file count is out of bounds")
        if len(self._candidates) >= self._max_candidates:
            raise SnapshotCandidateError("candidate capacity exhausted")

        entries: list[SnapshotFile] = []
        paths: set[str] = set()
        total = 0
        hasher = hashlib.sha256()
        for raw_path, raw_content in files:
            path = self._path(raw_path)
            if path in paths:
                raise SnapshotCandidateError("duplicate snapshot path")
            paths.add(path)
            if not isinstance(raw_content, bytes):
                raise SnapshotCandidateError("snapshot content must be bytes")
            total += len(raw_content)
            if total > self._max_total_bytes:
                raise SnapshotCandidateError("snapshot exceeds byte capacity")
            digest = hashlib.sha256(raw_content).hexdigest()
            encoded_path = path.encode("utf-8")
            hasher.update(len(encoded_path).to_bytes(4, "big"))
            hasher.update(encoded_path)
            hasher.update(len(raw_content).to_bytes(8, "big"))
            hasher.update(raw_content)
            entries.append(SnapshotFile(path, raw_content, digest))
        candidate = SnapshotCandidate(
            candidate_id=uuid.uuid4().hex,
            base_revision=base_revision,
            digest=hasher.hexdigest(),
            files=tuple(entries),
            expires_at=now + self._ttl_seconds,
        )
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def get(self, candidate_id: str) -> SnapshotCandidate:
        """Return a live candidate or reject an expired/unknown identity."""
        self._expire(time.monotonic())
        try:
            return self._candidates[candidate_id]
        except KeyError as exc:
            raise SnapshotCandidateError("candidate not found or expired") from exc

    def discard(self, candidate_id: str) -> None:
        self._candidates.pop(candidate_id, None)
