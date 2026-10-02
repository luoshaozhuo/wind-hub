"""Collector 专属异常。

跨进程共享的稳定异常定义来自 wind-hub-core；本模块只定义 Sink 等
Collector 专属运行边界的错误类型。
"""

from __future__ import annotations

from wind_hub_core.model.errors import (
    CommandError,
    ConfigError,
    OperationTimeoutError,
    ProtocolError,
    WindHubError,
)


class SinkError(WindHubError):
    """Sink 打开、写入、flush 或外部连接失败。"""


__all__ = [
    "WindHubError",
    "ConfigError",
    "ProtocolError",
    "CommandError",
    "OperationTimeoutError",
    "SinkError",
]
