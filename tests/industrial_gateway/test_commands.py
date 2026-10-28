"""Commander 三态控制契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from industrial_gateway.application.command_service import (
    CommanderProxy,
    CommandOutcome,
    CommandRequest,
    CommandResult,
    CommandState,
    CommandTarget,
)
from industrial_gateway.domain import PointWrite
from industrial_gateway.infrastructure.commander.journal import CommandJournal

pytestmark = pytest.mark.unit


class FakeCommander:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.requests: list[CommandRequest] = []

    async def write_point(self, request: CommandRequest, *, timeout: float) -> CommandResult:
        self.requests.append(request)
        return CommandResult(outcome=self.outcome)


async def test_confirmed_success_allows_repeated_same_value() -> None:
    fake = FakeCommander(CommandOutcome.CONFIRMED_SUCCESS)
    proxy = CommanderProxy(fake, {("t1", "command"): CommandTarget("physical", "setpoint")})
    command = PointWrite("t1", "command", 42, "dc-ems")
    await proxy(command)
    await proxy(command)
    assert len(fake.requests) == 2
    assert fake.requests[0].command_id != fake.requests[1].command_id
    assert fake.requests[0].device_id == "physical"
    assert proxy.state("t1", "command") is CommandState.READY


async def test_unknown_blocks_subsequent_writes() -> None:
    fake = FakeCommander(CommandOutcome.UNKNOWN)
    proxy = CommanderProxy(fake, {("t1", "command"): CommandTarget("physical", "setpoint")})
    command = PointWrite("t1", "command", 42, "collector")
    with pytest.raises(RuntimeError, match="unknown"):
        await proxy(command)
    assert proxy.state("t1", "command") is CommandState.UNKNOWN
    with pytest.raises(RuntimeError, match="locked"):
        await proxy(command)
    assert len(fake.requests) == 1


async def test_confirmed_failure_does_not_lock() -> None:
    fake = FakeCommander(CommandOutcome.CONFIRMED_FAILURE)
    proxy = CommanderProxy(fake, {("t1", "command"): CommandTarget("physical", "setpoint")})
    with pytest.raises(RuntimeError, match="rejected"):
        await proxy(PointWrite("t1", "command", 2, "dc-ems"))
    assert proxy.state("t1", "command") is CommandState.READY


@pytest.mark.parametrize("value", [-(1 << 63) - 1, 1 << 64])
async def test_invalid_scalar_does_not_enter_executing_state(value: int) -> None:
    fake = FakeCommander(CommandOutcome.CONFIRMED_SUCCESS)
    proxy = CommanderProxy(fake, {("t1", "command"): CommandTarget("physical", "setpoint")})
    with pytest.raises(ValueError, match="64-bit range"):
        await proxy(PointWrite("t1", "command", value, "dc-ems"))
    assert fake.requests == []
    assert proxy.state("t1", "command") is CommandState.READY


def test_journal_failed_begin_latches_all_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = CommandJournal(tmp_path / "journal.json")

    def fail_save() -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(journal, "_save", fail_save)
    with pytest.raises(OSError, match="fsync failed"):
        journal.begin("point-a", "cmd-1")
    assert journal.blocked("point-a")
    assert journal.blocked("point-b")
    with pytest.raises(RuntimeError, match="locked"):
        journal.confirmed("point-a")


def test_journal_failed_confirmation_remains_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = CommandJournal(tmp_path / "journal.json")
    journal.begin("point-a", "cmd-1")

    def fail_save() -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(journal, "_save", fail_save)
    with pytest.raises(OSError, match="fsync failed"):
        journal.confirmed("point-a")
    assert journal.blocked("point-a")
    assert journal.blocked("point-b")
