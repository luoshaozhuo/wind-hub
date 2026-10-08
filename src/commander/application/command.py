"""Commander 即时写命令的用例模型。

Command 的 ``command_id`` 是进程内幂等键；同一逻辑操作的重试必须复用
相同 command_id，且命令内容（device/point/value）必须一致——Dispatcher
对同键不同内容返回幂等冲突。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeAlias

CommandValue: TypeAlias = float | int | bool | str
"""写命令允许的业务标量值；类型合法性由点表与协议 Driver 校验。"""


@dataclass(frozen=True, slots=True)
class Command:
    """下发给设备的一条逻辑写命令。

    Attributes:
        command_id: 调用方生成的幂等键。
        device_id: 目标设备标识。
        point_id: 目标点表内 point_id。
        value: 业务（工程）值；写入前由会话按点表 scale/offset 逆变换为
            协议原始值。
        timeout: 写操作最大等待时间（秒）；<= 0 时使用进程默认写超时。
        issued_at: 命令创建时间（UTC）。
    """

    command_id: str
    device_id: str
    point_id: str
    value: CommandValue
    timeout: float = 5.0
    issued_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class CommandResult:
    """单条 Command 的执行结果。

    Attributes:
        command_id: 回显原始 command_id。
        success: 设备写入被确认时为 True。
        error: 失败时的可读错误原因。
        finished_at: 执行完成时间（UTC）。
    """

    command_id: str
    success: bool
    error: str | None = None
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))
