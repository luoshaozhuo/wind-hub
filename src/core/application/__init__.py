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
    ListenHandle,
    ProtocolPort,
    ProtocolSampleCallback,
    SubscriptionHandle,
)
from .protocol_contract import (
    ConnectionHealth,
    InterrogationRequest,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    TimestampSource,
    WritableScalar,
    WriteOutcome,
    validate_read_many_results,
)
from .protocol_registry import ProtocolFactory, ProtocolRegistry
from .watchdog import WatchdogUseCase

__all__ = [
    "ConfigError",
    "ConfigPort",
    "ConnectionHealth",
    "InterrogationRequest",
    "CoreError",
    "PointScalar",
    "ProtocolCapability",
    "ProtocolCapabilityError",
    "ProtocolConnectionError",
    "ProtocolError",
    "ProtocolFactory",
    "ProtocolPort",
    "ProtocolRegistry",
    "ProtocolSample",
    "ProtocolSampleCallback",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "WriteOutcome",
    "Quality",
    "SubscriptionHandle",
    "ListenHandle",
    "TimestampSource",
    "WritableScalar",
    "WatchdogUseCase",
    "validate_read_many_results",
]
