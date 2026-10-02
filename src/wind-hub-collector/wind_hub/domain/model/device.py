"""设备端点与只读运行状态领域模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Endpoint(BaseModel):
    """设备连接端点。

    extensions 保存协议特有参数；Any 仅对应协议动态配置边界，具体 Driver 会将其
    收敛为强类型协议配置。
    """

    host: str
    """设备 IP 或主机名。"""

    port: int
    """协议连接端口。"""

    extensions: dict[str, Any] = Field(default_factory=dict)
    """协议扩展参数，例如 common_addr、unit_id、target_net_id。"""


class DeviceInfo(BaseModel):
    """查询接口返回的设备运行状态快照。"""

    device_id: str
    """设备稳定标识。"""

    protocol: str
    """当前使用的协议 Driver 名称。"""

    connected: bool
    """协议 adapter health() 为 healthy 时为 True。"""

    last_seen: datetime | None = None
    """最近一次成功读取时间；尚未追踪时为 None。"""

    consecutive_failures: int = 0
    """连续连接失败次数；设备恢复后归零。"""

    last_error: str | None = None
    """最近一次失败的简要描述。"""
