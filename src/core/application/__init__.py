"""Shared Core Application 公共门面。

Application 只暴露用例编排、应用契约、错误和 Ports。
"""

from .config_contract import (
    ConfigRevision,
    ConfigRevisionConflict,
    CoreConfigArtifact,
    CoreConfigPreview,
    CoreConfigUpdateResult,
    StoredCoreConfig,
)
from .config_service import CoreConfigService
from .errors import ConfigError, CoreError, ProtocolCapabilityError, ProtocolError
from .port import (
    CoreConfigCodecPort,
    CoreConfigPort,
    CoreConfigRepositoryPort,
    CoreConfigValidatorPort,
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
    "ConfigRevision",
    "ConfigRevisionConflict",
    "ConnectionHealth",
    "CoreConfigArtifact",
    "CoreConfigCodecPort",
    "CoreConfigPort",
    "CoreConfigPreview",
    "CoreConfigRepositoryPort",
    "CoreConfigService",
    "CoreConfigUpdateResult",
    "CoreConfigValidatorPort",
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
    "StoredCoreConfig",
    "SubscriptionHandle",
    "WritableScalar",
]
