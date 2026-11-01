"""Commander command identity, state transitions and immutable results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite

from core.domain.sample_value import WritableScalar


class CommandState(StrEnum):
    ACCEPTED = "ACCEPTED"
    CONNECTING = "CONNECTING"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    INTERRUPTED = "INTERRUPTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class CommandOutcome(StrEnum):
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    CONFIRMED_FAILURE = "CONFIRMED_FAILURE"
    UNKNOWN = "UNKNOWN"
    NOT_SENT = "NOT_SENT"


TERMINAL_STATES = frozenset(
    {
        CommandState.SUCCEEDED,
        CommandState.FAILED,
        CommandState.UNKNOWN,
        CommandState.INTERRUPTED,
        CommandState.EXPIRED,
        CommandState.CANCELLED,
    }
)

_ALLOWED = {
    CommandState.ACCEPTED: frozenset(
        {
            CommandState.CONNECTING,
            CommandState.EXECUTING,
            CommandState.FAILED,
            CommandState.EXPIRED,
            CommandState.CANCELLED,
            CommandState.INTERRUPTED,
        }
    ),
    CommandState.CONNECTING: frozenset(
        {
            CommandState.EXECUTING,
            CommandState.FAILED,
            CommandState.EXPIRED,
            CommandState.CANCELLED,
            CommandState.INTERRUPTED,
        }
    ),
    CommandState.EXECUTING: frozenset(
        {
            CommandState.SUCCEEDED,
            CommandState.FAILED,
            CommandState.UNKNOWN,
            CommandState.EXPIRED,
        }
    ),
}


class CommandStateError(ValueError):
    """Illegal command transition or contradictory terminal evidence."""


def validate_transition(
    previous: CommandState,
    current: CommandState,
    outcome: CommandOutcome | None = None,
) -> None:
    if current not in _ALLOWED.get(previous, frozenset()):
        raise CommandStateError(f"illegal command transition: {previous} -> {current}")
    expected = {
        CommandState.SUCCEEDED: CommandOutcome.CONFIRMED_SUCCESS,
        CommandState.FAILED: CommandOutcome.CONFIRMED_FAILURE,
        CommandState.UNKNOWN: CommandOutcome.UNKNOWN,
        CommandState.INTERRUPTED: CommandOutcome.NOT_SENT,
        CommandState.EXPIRED: CommandOutcome.NOT_SENT,
        CommandState.CANCELLED: CommandOutcome.NOT_SENT,
    }.get(current)
    if expected is not None and outcome != expected:
        raise CommandStateError(f"{current} requires outcome {expected}")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    device_id: str
    point_id: str
    value: WritableScalar
    config_reference: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("device_id", "point_id", "config_reference"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.value, (bool, int, float, str)):
            raise ValueError("value must be a writable protocol scalar")
        if isinstance(self.value, float) and not isfinite(self.value):
            raise ValueError("value must be finite")
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must include timezone")


@dataclass(frozen=True, slots=True)
class AcceptedCommand:
    command_id: str
    request_id: str
    caller_id: str
    spec: CommandSpec
    received_at: datetime
    receive_seq: int


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: AcceptedCommand
    state: CommandState
    outcome: CommandOutcome | None
    error_code: str | None
    error_message: str | None
    completed_at: datetime | None


def utc_now() -> datetime:
    return datetime.now(UTC)
