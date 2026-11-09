"""In-memory command lifecycle with asynchronous durable-history lookups."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from functools import partial
from typing import Any, TypeVar

from commander.application.journal_port import JournalPort
from commander.application.persistence import BufferedJournal
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
)

_T = TypeVar("_T")


class CommandControl:
    """Keep volatile command state; await durable T1 before allowing device I/O."""

    def __init__(
        self,
        repository: JournalPort,
        writer: Executor,
        buffer: BufferedJournal,
        *,
        accept_timeout: float = 10.0,
    ) -> None:
        if accept_timeout <= 0:
            raise ValueError("accept_timeout must be positive")
        self._repository = repository
        self._writer = writer
        self._buffer = buffer
        self._accept_timeout = accept_timeout

    async def _run(self, method: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        loop = asyncio.get_running_loop()
        work = loop.run_in_executor(self._writer, partial(method, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await work
            raise

    async def accept(
        self,
        commands: Sequence[CommandSpec],
        *,
        caller_id: str,
        request_id: str,
        operator_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[AcceptedCommand, ...]:
        accepted = self._buffer.accept(
            commands,
            caller_id=caller_id,
            request_id=request_id,
            operator_id=operator_id,
            trace_id=trace_id,
        )
        # An acknowledged control must have a durable request identity before
        # any caller can schedule physical I/O.
        try:
            await asyncio.wait_for(
                self._buffer.wait_persisted(
                    [command.command_id for command in accepted]
                ),
                timeout=self._accept_timeout,
            )
        except TimeoutError:
            # Nothing has been queued or sent. Preserve a conservative
            # terminal event even if the asynchronous T1 flush later succeeds.
            self._buffer.transition_many(
                tuple(
                    (
                        command.command_id,
                        CommandState.INTERRUPTED,
                        CommandOutcome.NOT_SENT,
                        "PERSISTENCE_TIMEOUT",
                    )
                    for command in accepted
                )
            )
            raise
        return accepted

    async def connecting(self, command_id: str) -> None:
        self._buffer.transition(command_id, CommandState.CONNECTING)

    async def executing(self, command_id: str) -> None:
        self._buffer.transition(command_id, CommandState.EXECUTING)

    async def executing_many(self, command_ids: Sequence[str]) -> None:
        self._buffer.transition_many(
            tuple((command_id, CommandState.EXECUTING, None, None) for command_id in command_ids)
        )

    async def complete_many(
        self,
        changes: Sequence[tuple[str, CommandState, CommandOutcome, str | None]],
    ) -> None:
        self._buffer.transition_many(changes)

    async def complete(
        self,
        command_id: str,
        state: CommandState,
        *,
        outcome: CommandOutcome,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._buffer.transition(
            command_id,
            state,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
        )

    async def get_command(self, command_id: str) -> CommandResult | None:
        cached = self._buffer.get_cached(command_id)
        if cached is not None:
            return cached
        return await self._run(self._repository.get, command_id)

    async def get_request(
        self, request_id: str, caller_id: str
    ) -> tuple[CommandResult, ...]:
        cached = self._buffer.get_request_cached(request_id, caller_id)
        if cached is not None:
            # A provisional in-memory T1 must never appear as accepted
            # before its batch is durably committed.
            await asyncio.wait_for(
                self._buffer.wait_persisted(
                    [item.command.command_id for item in cached]
                ),
                timeout=self._accept_timeout,
            )
            return self._buffer.get_request_cached(request_id, caller_id) or cached
        return await self._run(self._repository.get_request, request_id, caller_id)

    async def recover(self) -> tuple[str, ...]:
        return await self._run(self._repository.recover)
