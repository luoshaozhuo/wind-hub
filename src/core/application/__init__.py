"""Shared Core Application。

只保留 Collector、Commander、Server 稳定共享的配置用例、配置端口、
最小设备协议 Port 与协议数据契约。
"""

from .config import (
    ConfigRevision,
    ConfigRevisionConflict,
    CoreConfigArtifact,
    CoreConfigDiff,
    CoreConfigSnapshot,
    IndexDiff,
    StoredCoreConfig,
    compute_core_config_diff,
    validate_core_config,
)
from .config.service import (
    CoreConfigPreview,
    CoreConfigService,
    CoreConfigUpdateResult,
)
from .errors import ConfigError, CoreError, ProtocolError
from .port import (
    CoreConfigCodecPort,
    CoreConfigRepositoryPort,
    CoreConfigValidatorPort,
    InterrogatableProtocolPort,
    ProtocolPort,
    ProtocolSampleCallback,
    SubscribableProtocolPort,
    SubscriptionHandle,
)
from .protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WritableScalar,
)

__all__ = [
    "ConfigError",
    "ConfigRevision",
    "ConfigRevisionConflict",
    "ConnectionHealth",
    "CoreConfigArtifact",
    "CoreConfigCodecPort",
    "CoreConfigDiff",
    "CoreConfigPreview",
    "CoreConfigRepositoryPort",
    "CoreConfigService",
    "CoreConfigSnapshot",
    "CoreConfigUpdateResult",
    "CoreConfigValidatorPort",
    "CoreError",
    "IndexDiff",
    "InterrogatableProtocolPort",
    "PointScalar",
    "ProtocolError",
    "ProtocolPort",
    "ProtocolSample",
    "ProtocolSampleCallback",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "Quality",
    "StoredCoreConfig",
    "SubscribableProtocolPort",
    "SubscriptionHandle",
    "WritableScalar",
    "compute_core_config_diff",
    "validate_core_config",
]
