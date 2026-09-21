"""Unit tests for the CommandUseCase application usecase.

验证对象：``application/usecase/command.py`` 把指令下发委托给
:class:`~wind_hub.application.command_dispatcher.CommandDispatcher`——``send`` /
``send_batch`` 原样转发，幂等与超时由 CommandDispatcher 保证，服务本身不增加语义。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from wind_hub.application.command_dispatcher import CommandDispatcher
from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.domain.model.command import Command, CommandResult


def _command(command_id: str = "c1") -> Command:
    return Command(command_id=command_id, device_id="d1", point_id="p1", value=1.0)


def _result(command_id: str, success: bool = True) -> CommandResult:
    return CommandResult(command_id=command_id, success=success)


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


async def test_send_delegates_to_dispatcher() -> None:
    dispatcher = MagicMock(spec=CommandDispatcher)
    expected = _result("c1")
    dispatcher.send = AsyncMock(return_value=expected)

    result = await CommandUseCase(dispatcher).send(_command())

    dispatcher.send.assert_awaited_once()
    assert result is expected


# ---------------------------------------------------------------------------
# send_batch
# ---------------------------------------------------------------------------


async def test_send_batch_delegates_to_dispatcher() -> None:
    dispatcher = MagicMock(spec=CommandDispatcher)
    cmds = [_command("c1"), _command("c2")]
    expected = [_result("c1"), _result("c2", success=False)]
    dispatcher.send_batch = AsyncMock(return_value=expected)

    results = await CommandUseCase(dispatcher).send_batch(cmds)

    dispatcher.send_batch.assert_awaited_once()
    assert results == expected
    assert len(results) == 2
