"""Collector 专属异常。

共享异常定义统一来自 wind-hub-core；本模块只保留 Sink 边界的错误类型。
"""

from __future__ import annotations

from wind_hub_core.model.errors import WindHubError


class SinkError(WindHubError):
    """Sink 打开、写入、flush 或外部连接失败。"""


__all__ = ["SinkError"]
