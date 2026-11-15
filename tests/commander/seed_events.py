"""Test-only durable event seeding via the write-behind production contract."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4
from weakref import WeakKeyDictionary

from commander.application.persistence import BufferedJournal
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandSpec,
    CommandState,
)
from commander.infrastructure.persistence.event_repository import EventRepository

_BUFFERS: WeakKeyDictionary[EventRepository, BufferedJournal] = WeakKeyDictionary()


def _buffer(repository: EventRepository) -> BufferedJournal:
    buffer = _BUFFERS.get(repository)
    if buffer is None:
        buffer = BufferedJournal(initial_sequence=repository.latest_receive_seq())
        _BUFFERS[repository] = buffer
    return buffer


def _flush(repository: EventRepository, buffer: BufferedJournal) -> None:
    while buffer.pending_count:
        events = buffer.snapshot(500)
        repository.append_events(events)
        buffer.confirm(len(events))


def seed_accept(
    repository: EventRepository,
    specs: Sequence[CommandSpec],
    *,
    caller_id: str,
    request_id: str | None = None,
    operator_id: str | None = None,
    trace_id: str | None = None,
) -> tuple[AcceptedCommand, ...]:
    buffer = _buffer(repository)
    request_id = request_id or f"seed-{uuid4().hex}"
    commands = buffer.accept(
        specs,
        caller_id=caller_id,
        request_id=request_id,
        operator_id=operator_id,
        trace_id=trace_id,
    )
    _flush(repository, buffer)
    return commands


def seed_transition(
    repository: EventRepository,
    command_id: str,
    state: CommandState,
    *,
    outcome: CommandOutcome | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    buffer = _buffer(repository)
    buffer.transition(
        command_id,
        state,
        outcome=outcome,
        error_code=error_code,
        error_message=error_message,
    )
    _flush(repository, buffer)
