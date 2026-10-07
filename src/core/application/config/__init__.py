"""Shared Core 应用级配置模型与用例。"""

from .artifact import CoreConfigArtifact
from .device_connection import ConnectionEndpoint, DeviceConnection
from .diff import CoreConfigDiff, IndexDiff, compute_core_config_diff
from .revision import (
    ConfigRevision,
    ConfigRevisionConflict,
    StoredCoreConfig,
)
from .service import (
    CoreConfigPreview,
    CoreConfigService,
    CoreConfigUpdateResult,
)
from .snapshot import CoreConfigSnapshot
from .validation import validate_core_config

__all__ = [
    "ConfigRevision",
    "ConfigRevisionConflict",
    "ConnectionEndpoint",
    "CoreConfigArtifact",
    "CoreConfigDiff",
    "CoreConfigPreview",
    "CoreConfigService",
    "CoreConfigSnapshot",
    "CoreConfigUpdateResult",
    "DeviceConnection",
    "IndexDiff",
    "StoredCoreConfig",
    "compute_core_config_diff",
    "validate_core_config",
]
