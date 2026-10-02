"""Unit tests for the CommandUseCase application usecase.

验证对象：``application/usecase/command.py`` 把指令下发委托给
:class:`~wind_hub.application.command_dispatcher.CommandDispatcher`——``send`` /
``send_batch`` 原样转发，幂等与超时由 CommandDispatcher 保证，服务本身不增加语义。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from wind_hub.application.command_dispatcher import CommandDispatcher
from wind_hub.application.runtime.runtime import Runtime
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
    runtime = MagicMock(spec=Runtime)
    runtime.devices = {"d1": object()}
    runtime.ensure_connected = AsyncMock(return_value=True)
    expected = _result("c1")
    dispatcher.get_cached = MagicMock(return_value=None)
    dispatcher.send = AsyncMock(return_value=expected)

    result = await CommandUseCase(dispatcher, runtime).send(_command())

    runtime.ensure_connected.assert_awaited_once_with("d1", force=True)
    dispatcher.send.assert_awaited_once()
    assert result is expected


async def test_send_cached_result_skips_reconnect_and_dispatch() -> None:
    dispatcher = MagicMock(spec=CommandDispatcher)
    runtime = MagicMock(spec=Runtime)
    runtime.devices = {"d1": object()}
    runtime.ensure_connected = AsyncMock(return_value=False)
    cached = _result("c1")
    dispatcher.get_cached = MagicMock(return_value=cached)
    dispatcher.send = AsyncMock()

    result = await CommandUseCase(dispatcher, runtime).send(_command())

    assert result is cached
    runtime.ensure_connected.assert_not_awaited()
    dispatcher.send.assert_not_awaited()


async def test_send_connection_failure_does_not_write() -> None:
    dispatcher = MagicMock(spec=CommandDispatcher)
    runtime = MagicMock(spec=Runtime)
    runtime.devices = {"d1": object()}
    runtime.ensure_connected = AsyncMock(return_value=False)
    dispatcher.get_cached = MagicMock(return_value=None)
    dispatcher.send = AsyncMock()

    result = await CommandUseCase(dispatcher, runtime).send(_command())

    assert result.success is False
    assert "not connected" in (result.error or "")
    runtime.ensure_connected.assert_awaited_once_with("d1", force=True)
    dispatcher.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# send_batch
# ---------------------------------------------------------------------------


async def test_send_batch_delegates_to_dispatcher() -> None:
    dispatcher = MagicMock(spec=CommandDispatcher)
    runtime = MagicMock(spec=Runtime)
    runtime.devices = {"d1": object()}
    runtime.ensure_connected = AsyncMock(return_value=True)
    cmds = [_command("c1"), _command("c2")]
    expected = [_result("c1"), _result("c2", success=False)]
    dispatcher.get_cached = MagicMock(return_value=None)
    dispatcher.send = AsyncMock(side_effect=expected)

    results = await CommandUseCase(dispatcher, runtime).send_batch(cmds)

    assert runtime.ensure_connected.await_count == 2
    assert dispatcher.send.await_count == 2
    assert results == expected
    assert len(results) == 2
