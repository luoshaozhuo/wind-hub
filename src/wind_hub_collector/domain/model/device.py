"""Collector 查询侧设备运行状态模型。

静态设备端点已迁入 wind-hub-core；本模块只保留 Collector Runtime 派生的
设备状态快照，不承载跨进程共享配置语义。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


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
