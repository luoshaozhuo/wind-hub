"""兼容导入层；Command 领域模型的唯一实现位于 wind-hub-core。"""

from wind_hub_core.model.command import Command, CommandResult

__all__ = ["Command", "CommandResult"]
