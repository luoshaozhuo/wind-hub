"""Command use case——指令下发的应用编排。

将 :class:`~wind_hub.application.command_dispatcher.CommandDispatcher`
包装为应用层用例：``send`` / ``send_batch`` 直接委托，幂等与超时均由
CommandDispatcher 保证（见其 docstring）。
"""

from __future__ import annotations

from wind_hub.application.command_dispatcher import CommandDispatcher
from wind_hub.domain.model.command import Command, CommandResult


class CommandUseCase:
    """指令下发用例——委托给 :class:`CommandDispatcher`。

    不做额外的权限/审计（这些属于更高层），只把占位替换为对
    CommandDispatcher 的真实调用：协议级失败内联到
    ``CommandResult.success=False``，派发失败（未知设备）同样内联
    而非抛异常。
    """

    def __init__(self, dispatcher: CommandDispatcher) -> None:
        self._dispatcher = dispatcher

    async def send(self, cmd: Command) -> CommandResult:
        """下发单条指令，等待结果。"""
        return await self._dispatcher.send(cmd)

    async def send_batch(self, cmds: list[Command]) -> list[CommandResult]:
        """并发下发多条指令，按输入顺序返回结果。"""
        return await self._dispatcher.send_batch(cmds)
