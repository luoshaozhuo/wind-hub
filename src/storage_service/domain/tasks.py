"""Task lifecycle value objects shared across Storage application and SQLite."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    kind: str
    wind_farm_id: str
    owner_id: str
    state: str
    created_at: str
    finished_at: str | None
    artifact_id: str
    size_bytes: int
    sha256: str
    error_code: str
    message: str
