"""设备写指令及其执行结果领域模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Command(BaseModel):
    """下发给设备的一条逻辑写指令。

    command_id 是幂等键；同一逻辑操作重试必须复用相同 command_id。

    Attributes:
        value: 待写值。Any 仅表示跨协议点类型的联合值，实际类型由点表和
            ProtocolPort 校验。
    """

    command_id: str
    """调用方生成的幂等键。"""

    device_id: str
    """目标设备标识。"""

    point_id: str
    """目标 point_id。"""

    value: Any
    """待写值；类型必须符合点表定义。"""

    timeout: float = 5.0
    """写操作最大等待时间，单位秒。"""

    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """命令创建时间，UTC。"""


class CommandResult(BaseModel):
    """单条 Command 的执行结果。"""

    command_id: str
    """回显原始 command_id。"""

    success: bool
    """设备写入被确认时为 True。"""

    error: str | None = None
    """失败时的可读错误原因。"""

    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """执行完成时间，UTC。"""
