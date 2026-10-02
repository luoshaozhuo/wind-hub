"""跨进程共享的设备静态领域模型。

这里只表达设备连接端点等与具体进程 Runtime 无关的稳定模型；连接生命周期、
采集状态和查询快照由各可执行组件自行维护。
"""

from __future__ import annotations

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
