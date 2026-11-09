"""Commander command identity, state transitions and immutable results.

The domain records distinct terminal states for query and recovery. The event
journal persists every terminal transition as event_type=COMPLETED, while its
state and outcome columns preserve the precise terminal classification:
SUCCEEDED/CONFIRMED_SUCCESS, FAILED/CONFIRMED_FAILURE, UNKNOWN/UNKNOWN,
INTERRUPTED/NOT_SENT, EXPIRED/NOT_SENT and CANCELLED/NOT_SENT.
This is a projection, not a second independent state machine.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
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
            CommandState.INTERRUPTED,
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
        if not isinstance(self.value, bool | int | float | str):
            raise ValueError("value must be a writable protocol scalar")
        if isinstance(self.value, float) and not isfinite(self.value):
            raise ValueError("value must be finite")
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must include timezone")


def request_fingerprint(specs: Sequence[CommandSpec]) -> str:
    """Bind request order, scalar type, expiry and expected revisions."""
    items = [
        {
            "device_id": item.device_id,
            "point_id": item.point_id,
            "value": item.value,
            "expected_revision": item.config_reference,
            "expires_at": (
                item.expires_at.astimezone(UTC).isoformat()
                if item.expires_at is not None
                else None
            ),
        }
        for item in specs
    ]
    payload = json.dumps(
        items, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    updated_at: datetime | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)
