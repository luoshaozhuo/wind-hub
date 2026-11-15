"""Commander application use case for idempotent command-batch submission."""

from __future__ import annotations

from dataclasses import dataclass

from commander.application.service_lifecycle import CommanderService
from commander.domain.command import (
    TERMINAL_STATES,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
)


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    request_id: str
    command_ids: tuple[str, ...]
    state: str


def request_state(results: tuple[CommandResult, ...]) -> str:
    """Aggregate terminal and uncertain command outcomes for one request."""
    if any(result.state not in TERMINAL_STATES for result in results):
        if all(result.state is CommandState.ACCEPTED for result in results):
            return "ACCEPTED"
        return "EXECUTING"
    outcomes = {result.outcome for result in results}
    if CommandOutcome.UNKNOWN in outcomes:
        return "UNKNOWN"
    if outcomes == {CommandOutcome.CONFIRMED_SUCCESS}:
        return "SUCCEEDED"
    if outcomes == {CommandOutcome.NOT_SENT}:
        return "NOT_SENT"
    if outcomes <= {CommandOutcome.CONFIRMED_FAILURE, CommandOutcome.NOT_SENT}:
        return "FAILED"
    return "PARTIAL"


class CommanderSubmission:
    """Authorize at transport boundary, then atomically accept one request."""

    def __init__(self, service: CommanderService) -> None:
        self._service = service

    async def submit(
        self, specs: tuple[CommandSpec, ...], *, caller: str, request_id: str
    ) -> SubmissionReceipt:
        if not request_id.strip():
            raise ValueError("request_id is required")
        if not specs:
            raise ValueError("commands must not be empty")
        admitted = await self._service.executor.accept(
            specs, caller_id=caller, request_id=request_id
        )
        results = await self._service.control.get_request(request_id, caller)
        return SubmissionReceipt(
            request_id=request_id,
            command_ids=tuple(item.command_id for item in admitted),
            state=request_state(results),
        )
