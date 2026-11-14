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

    def get_request(self, request_id: str, caller_id: str) -> tuple[CommandResult, ...]: ...

    def recover(self) -> tuple[str, ...]: ...

    def latest_recovery_epoch(self, device_id: str) -> int: ...

    def recovery_decision(
        self, caller_id: str, request_id: str
    ) -> tuple[str, str, str, str, int, str] | None: ...

    def commit_recovery(
        self,
        *,
        caller_id: str,
        request_id: str,
        device_id: str,
        revision: str,
        evidence_digest: str,
        token_id: str,
        fencing_epoch: int,
        old_instance: str,
        new_instance: str,
    ) -> str: ...

    def latest_receive_seq(self) -> int: ...

    def recovery_blocked_devices(self) -> frozenset[str]: ...

    def append_events(self, events: Sequence[dict[str, Any]]) -> None: ...

    def pending_events(self, limit: int = 100) -> tuple[dict[str, Any], ...]: ...

    def mark_published(self, event_id: str) -> None: ...

    def record_publish_failure(self, event_id: str) -> None: ...

    def cleanup(self, limit: int = 100) -> int: ...

    def metrics(self) -> JournalMetrics: ...

    def close(self) -> None: ...
