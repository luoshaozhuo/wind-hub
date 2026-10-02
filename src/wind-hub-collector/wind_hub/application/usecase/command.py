"""Command use case——指令下发的应用编排。

将 :class:`~wind_hub.application.command_dispatcher.CommandDispatcher`
包装为应用层用例：显式写入前先要求 Runtime 立即确认设备连接；连接可用后
再委托 CommandDispatcher 执行幂等、超时和协议写入。
"""

from __future__ import annotations

import asyncio

from wind_hub.application.command_dispatcher import CommandDispatcher
from wind_hub.application.runtime.runtime import Runtime
from wind_hub.domain.model.command import Command, CommandResult


class CommandUseCase:
    """指令下发用例——委托给 :class:`CommandDispatcher`。

    不做额外的权限/审计（这些属于更高层）。已注册设备写入前通过
    Runtime 强制执行一次连接保证；随后委托 CommandDispatcher：协议级失败内联到
    ``CommandResult.success=False``，派发失败（未知设备）同样内联
    而非抛异常。
    """

    def __init__(self, dispatcher: CommandDispatcher, runtime: Runtime) -> None:
        self._dispatcher = dispatcher
        self._runtime = runtime

    async def send(self, cmd: Command) -> CommandResult:
        """下发单条指令并等待执行结果。

        Args:
            cmd: 待下发 Command。

        Returns:
            CommandResult；连接失败、协议失败、未知设备和超时均收敛为失败结果。
        """
        cached = self._dispatcher.get_cached(cmd.command_id)
        if cached is not None:
            return cached
        if cmd.device_id in self._runtime.devices:
            if not await self._runtime.ensure_connected(cmd.device_id, force=True):
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error=f"device '{cmd.device_id}' is not connected",
                )
        return await self._dispatcher.send(cmd)

    async def send_batch(self, cmds: list[Command]) -> list[CommandResult]:
        """并发下发多条指令。

        Args:
            cmds: 待下发命令列表。

        Returns:
            与输入顺序一致的 CommandResult 列表；单条失败不取消同批其他命令。
        """
        return list(await asyncio.gather(*(self.send(cmd) for cmd in cmds)))
