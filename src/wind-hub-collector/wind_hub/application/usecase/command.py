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
        """下发单条指令并等待执行结果。

        Args:
            cmd: 待下发 Command。

        Returns:
            CommandResult；协议失败、未知设备和超时均由 Dispatcher 收敛为失败结果。
        """
        return await self._dispatcher.send(cmd)

    async def send_batch(self, cmds: list[Command]) -> list[CommandResult]:
        """并发下发多条指令。

        Args:
            cmds: 待下发命令列表。

        Returns:
            与输入顺序一致的 CommandResult 列表；单条失败不取消同批其他命令。
        """
        return await self._dispatcher.send_batch(cmds)
