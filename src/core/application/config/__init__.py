"""Shared Core 应用级配置模型。"""

from .artifact import CoreConfigArtifact
from .diff import CoreConfigDiff, IndexDiff, compute_core_config_diff
from .protocol_options import (
    ProtocolOptions,
    ProtocolOptionValue,
)
from .revision import (
    ConfigRevision,
    ConfigRevisionConflict,
    StoredCoreConfig,
)
from .snapshot import CoreConfigSnapshot
from .validation import validate_core_config

__all__ = [
    "ConfigRevision",
    "ConfigRevisionConflict",
    "CoreConfigArtifact",
    "CoreConfigDiff",
    "CoreConfigSnapshot",
    "IndexDiff",
    "ProtocolOptionValue",
    "ProtocolOptions",
    "StoredCoreConfig",
    "compute_core_config_diff",
    "validate_core_config",
]
