"""Commander 即时写入用例。"""

from __future__ import annotations

from wind_hub_commander.dispatcher import CommandDispatcher
from wind_hub_commander.runtime import CommanderRuntime
from wind_hub_core.model.command import Command, CommandResult


class CommandUseCase:
    """显式确认连接后执行即时设备写入。"""

    def __init__(self, runtime: CommanderRuntime, dispatcher: CommandDispatcher) -> None:
        self._runtime = runtime
        self._dispatcher = dispatcher

    async def send(self, command: Command) -> CommandResult:
        """执行单条写入，连接失败收敛为 CommandResult。"""
        cached = self._dispatcher.get_cached(command.command_id)
        if cached is not None:
            return cached
        async with self._runtime.operation():
            try:
                self._runtime.device(command.device_id)
            except KeyError:
                return CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error=f"unknown device '{command.device_id}'",
                )
            if not await self._runtime.ensure_connected(command.device_id):
                return CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error=f"device '{command.device_id}' is not connected",
                )
            return await self._dispatcher.send(command)

