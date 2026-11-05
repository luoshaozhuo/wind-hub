"""Application-owned runtime repository contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from commander.domain.command import CommandResult


@dataclass(frozen=True, slots=True)
class JournalMetrics:
    used_bytes: int
    accepted_total: int
    pending_events: int
    oldest_pending_age_ms: float
    terminal_counts: tuple[tuple[str, int], ...]


class JournalPort(Protocol):
    @property
    def operational(self) -> bool: ...

    def get(self, command_id: str) -> CommandResult | None: ...

    def get_request(self, request_id: str) -> tuple[CommandResult, ...]: ...

    def recover(self) -> tuple[str, ...]: ...

    def latest_receive_seq(self) -> int: ...

    def append_events(self, events: Sequence[dict[str, Any]]) -> None: ...

    def pending_events(self, limit: int = 100) -> tuple[dict[str, Any], ...]: ...

    def mark_published(self, event_id: str) -> None: ...

    def record_publish_failure(self, event_id: str) -> None: ...

    def cleanup(self, limit: int = 100) -> int: ...

    def metrics(self) -> JournalMetrics: ...

    def close(self) -> None: ...
