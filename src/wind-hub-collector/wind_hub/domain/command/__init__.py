"""Command — 指令领域模型（Command / CommandResult）。

命令分发器（CommandDispatcher）位于 application 层
（``wind_hub.application.command_dispatcher``）——它依赖 application
runtime 的 ``Device``，不再属于 domain。
"""

from wind_hub.domain.model.command import Command, CommandResult

__all__ = ["Command", "CommandResult"]
