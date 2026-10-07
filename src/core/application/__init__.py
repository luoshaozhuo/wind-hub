"""Shared Core Application 公共门面。

Application 只暴露流程边界、协议契约与应用错误。
"""

from .errors import ConfigError, CoreError, ProtocolCapabilityError, ProtocolError
from .port import (
    ConfigRepositoryPort,
    ProtocolPort,
    ProtocolSampleCallback,
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
    WritableScalar,
)

__all__ = [
    "ConfigError",
    "ConfigRepositoryPort",
    "ConnectionHealth",
    "CoreError",
    "PointScalar",
    "ProtocolCapability",
    "ProtocolCapabilityError",
    "ProtocolError",
    "ProtocolPort",
    "ProtocolSample",
    "ProtocolSampleCallback",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "Quality",
    "SubscriptionHandle",
    "WritableScalar",
]
