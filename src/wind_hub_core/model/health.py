"""跨进程共享的轻量健康状态模型。"""

from __future__ import annotations

from pydantic import BaseModel


class HealthStatus(BaseModel):
    """协议、Sink 或其他组件的轻量健康状态快照。"""

    healthy: bool
    """组件当前可正常工作时为 True。"""

    message: str | None = None
    """可选状态说明或错误摘要。"""
