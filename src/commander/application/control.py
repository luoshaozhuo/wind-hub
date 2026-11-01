"""Transport-independent command acceptance and durable lifecycle service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from functools import partial
from typing import Any, TypeVar

from commander.application.journal_port import JournalPort
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
)


_T = TypeVar("_T")


class CommandControl:
    """Only persist verified decisions; device I/O belongs to later stages."""

    def __init__(self, repository: JournalPort, writer: Executor) -> None:
        self._repository = repository
        self._writer = writer

    async def _run(
        self, method: Callable[..., _T], *args: Any, **kwargs: Any
    ) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._writer, partial(method, *args, **kwargs)
        )

    async def accept(
        self,
        commands: Sequence[CommandSpec],
        *,
        caller_id: str,
        operator_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[AcceptedCommand, ...]:
        return await self._run(
            self._repository.accept,
            commands,
            caller_id=caller_id,
            operator_id=operator_id,
            trace_id=trace_id,
        )

    async def connecting(self, command_id: str) -> None:
        await self._run(self._repository.transition, command_id, CommandState.CONNECTING)

    async def executing(self, command_id: str) -> None:
        """Commit this boundary before any potentially effective field write."""
        await self._run(self._repository.transition, command_id, CommandState.EXECUTING)

    async def complete(
        self,
        command_id: str,
        state: CommandState,
        *,
        outcome: CommandOutcome,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._run(
            self._repository.transition,
            command_id,
            state,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
        )

    async def get_command(self, command_id: str) -> CommandResult | None:
        return await self._run(self._repository.get, command_id)

    async def get_request(self, request_id: str) -> tuple[CommandResult, ...]:
        return await self._run(self._repository.get_request, request_id)

    async def recover(self) -> tuple[str, ...]:
        """Must run before the future runtime starts accepting commands."""
        return await self._run(self._repository.recover)
