"""Shared Core Application 公共门面。

Application 只暴露共享协议边界、协议契约与应用错误。
"""

from .errors import ConfigError, CoreError, ProtocolCapabilityError, ProtocolError
from .port import ProtocolPort, ProtocolSampleCallback, SubscriptionHandle
from .protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WritableScalar,
)
from .protocol_registry import ProtocolFactory, ProtocolRegistry

__all__ = [
    "ConfigError",
    "ConnectionHealth",
    "CoreError",
    "PointScalar",
    "ProtocolCapability",
    "ProtocolCapabilityError",
    "ProtocolError",
    "ProtocolFactory",
    "ProtocolPort",
    "ProtocolRegistry",
    "ProtocolSample",
    "ProtocolSampleCallback",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "Quality",
    "SubscriptionHandle",
    "WritableScalar",
]
