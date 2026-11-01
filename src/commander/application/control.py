"""Transport-independent command acceptance and durable lifecycle service."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
)


class CommandJournal(Protocol):
    def accept(
        self,
        specs: Sequence[CommandSpec],
        *,
        caller_id: str,
        operator_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[AcceptedCommand, ...]: ...

    def transition(
        self,
        command_id: str,
        state: CommandState,
        *,
        outcome: CommandOutcome | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

    def get(self, command_id: str) -> CommandResult | None: ...

    def get_request(self, request_id: str) -> tuple[CommandResult, ...]: ...

    def recover(self) -> tuple[str, ...]: ...


class CommandControl:
    """Only persist verified decisions; device I/O belongs to later stages."""

    def __init__(self, repository: CommandJournal) -> None:
        self._repository = repository

    def accept(
        self,
        commands: Sequence[CommandSpec],
        *,
        caller_id: str,
        operator_id: str | None = None,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[AcceptedCommand, ...]:
        return self._repository.accept(
            commands,
            caller_id=caller_id,
            operator_id=operator_id,
            trace_id=trace_id,
        )

    def connecting(self, command_id: str) -> None:
        self._repository.transition(command_id, CommandState.CONNECTING)

    def executing(self, command_id: str) -> None:
        """Commit this boundary before any potentially effective field write."""
        self._repository.transition(command_id, CommandState.EXECUTING)

    def complete(
        self,
        command_id: str,
        state: CommandState,
        *,
        outcome: CommandOutcome,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._repository.transition(
            command_id,
            state,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
        )

    def get_command(self, command_id: str) -> CommandResult | None:
        return self._repository.get(command_id)

    def get_request(self, request_id: str) -> tuple[CommandResult, ...]:
        return self._repository.get_request(request_id)

    def recover(self) -> tuple[str, ...]:
        """Must run before the future runtime starts accepting commands."""
        return self._repository.recover()
