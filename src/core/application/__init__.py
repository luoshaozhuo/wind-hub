"""Shared Core Application 公共门面。

Application 暴露共享协议边界、协议契约、应用错误，以及统一配置服务
（read / save / validate）与共享语义配置 Diff 用例。
"""

from .config_service import ConfigService
from .diff_config import DiffConfigUseCase
from .errors import ConfigError, CoreError, ProtocolCapabilityError, ProtocolError
from .port import (
    TOPIC_CONFIG_TYPES,
    ConfigPort,
    ConfigSnapshot,
    ConfigSnapshotPort,
    ConfigTopic,
    ConfigValue,
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
    validate_read_many_results,
)
from .protocol_registry import ProtocolFactory, ProtocolRegistry

__all__ = [
    "TOPIC_CONFIG_TYPES",
    "ConfigError",
    "ConfigPort",
    "ConfigService",
    "ConfigSnapshot",
    "ConfigSnapshotPort",
    "ConfigTopic",
    "ConfigValue",
    "ConnectionHealth",
    "CoreError",
    "DiffConfigUseCase",
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
    "validate_read_many_results",
]
