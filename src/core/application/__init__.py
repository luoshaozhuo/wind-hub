"""Shared Core Application 公共门面。

Application 暴露共享协议边界、协议契约、应用错误与只读配置端口契约。
"""

from .errors import (
    ConfigError,
    CoreError,
    ProtocolCapabilityError,
    ProtocolConnectionError,
    ProtocolError,
)
from .port import (
    ConfigPort,
    ExclusiveOpenSinkPort,
    ProtocolPort,
    ProtocolSampleCallback,
    SinkPort,
    SubscriptionHandle,
)
from .protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    TimestampSource,
    WritableScalar,
    validate_read_many_results,
)
from .protocol_registry import ProtocolFactory, ProtocolRegistry

__all__ = [
    "ConfigError",
    "ConfigPort",
    "ConnectionHealth",
    "CoreError",
    "PointScalar",
    "ProtocolCapability",
    "ProtocolCapabilityError",
    "ProtocolConnectionError",
    "ProtocolError",
    "ProtocolFactory",
    "ProtocolPort",
    "SinkPort",
    "ExclusiveOpenSinkPort",
    "ProtocolRegistry",
    "ProtocolSample",
    "ProtocolSampleCallback",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "Quality",
    "SubscriptionHandle",
    "TimestampSource",
    "WritableScalar",
    "validate_read_many_results",
]
