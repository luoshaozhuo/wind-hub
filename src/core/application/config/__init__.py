"""Shared Core 的应用级配置模型、仓储与用例。"""

from .codec import CoreConfigArtifact, CoreConfigCodecPort
from .device_connection import ConnectionEndpoint, DeviceConnection
from .diff import CoreConfigDiff, IndexDiff, compute_core_config_diff
from .repository import (
    ConfigRevision,
    ConfigRevisionConflict,
    CoreConfigRepositoryPort,
    StoredCoreConfig,
)
from .service import CoreConfigService, CoreConfigUpdateResult
from .snapshot import CoreConfigSnapshot
from .validation import validate_core_config
from .validator import CoreConfigValidatorPort

__all__ = [
    "ConfigRevision",
    "ConfigRevisionConflict",
    "ConnectionEndpoint",
    "CoreConfigArtifact",
    "CoreConfigCodecPort",
    "CoreConfigDiff",
    "CoreConfigRepositoryPort",
    "CoreConfigService",
    "CoreConfigSnapshot",
    "CoreConfigUpdateResult",
    "CoreConfigValidatorPort",
    "DeviceConnection",
    "IndexDiff",
    "StoredCoreConfig",
    "compute_core_config_diff",
    "validate_core_config",
]
