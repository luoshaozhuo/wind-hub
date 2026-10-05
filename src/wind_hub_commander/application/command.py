"""Commander 即时写入服务。"""

from __future__ import annotations

import asyncio

from wind_hub_commander.dispatcher import CommandDispatcher
from wind_hub_core.model.command import Command, CommandResult


class CommanderCommandService:
    """Commander 即时设备写入入口。"""

    def __init__(self, dispatcher: CommandDispatcher) -> None:
        self._dispatcher = dispatcher

    async def send(self, command: Command) -> CommandResult:
        """执行单条写入；连接、幂等和错误收敛统一由 Dispatcher 负责。"""
        return await self._dispatcher.send(command)

    async def send_batch(self, commands: list[Command]) -> list[CommandResult]:
        """并发执行多条即时写命令。"""
        return list(await asyncio.gather(*(self.send(command) for command in commands)))
