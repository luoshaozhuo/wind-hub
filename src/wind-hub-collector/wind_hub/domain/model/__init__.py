"""Collector 领域模型聚合入口。

跨进程共享的 Point、Command、Endpoint 与通用异常来自 wind-hub-core；
本包仅保留 DeviceInfo、SinkError 和 Collector reload 等进程专属模型。
"""

from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.errors import SinkError
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.errors import (
    CommandError,
    ConfigError,
    OperationTimeoutError,
    ProtocolError,
    WindHubError,
)
from wind_hub_core.model.point import PointRef, PointValue, Quality

__all__ = [
    "PointRef",
    "PointValue",
    "Quality",
    "Command",
    "CommandResult",
    "Endpoint",
    "DeviceInfo",
    "WindHubError",
    "ConfigError",
    "ProtocolError",
    "SinkError",
    "CommandError",
    "OperationTimeoutError",
]
