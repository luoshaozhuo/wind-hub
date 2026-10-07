"""Shared Core 应用级配置模型。

这里只聚合配置数据模型、差异与纯校验；应用服务从
core.application.config.service 显式导入，避免与 port 形成循环依赖。
"""

from .artifact import CoreConfigArtifact
from .device_connection import ConnectionEndpoint, DeviceConnection
from .identities import ConnectionId
from .diff import CoreConfigDiff, IndexDiff, compute_core_config_diff
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
    "ConnectionEndpoint",
    "ConnectionId",
    "CoreConfigArtifact",
    "CoreConfigDiff",
    "CoreConfigSnapshot",
    "DeviceConnection",
    "IndexDiff",
    "StoredCoreConfig",
    "compute_core_config_diff",
    "validate_core_config",
]
