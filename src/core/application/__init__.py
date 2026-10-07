"""Shared Core Application。

仅保留 Collector、Commander、Server 共同依赖的应用级能力：
共享配置、设备通信会话、协议端口以及协议值归一化。
"""

from .config import (
    ConfigRevision,
    ConfigRevisionConflict,
    ConnectionEndpoint,
    CoreConfigArtifact,
    CoreConfigCodecPort,
    CoreConfigDiff,
    CoreConfigRepositoryPort,
    CoreConfigService,
    CoreConfigSnapshot,
    CoreConfigUpdateResult,
    DeviceConnection,
    IndexDiff,
    StoredCoreConfig,
    compute_core_config_diff,
    validate_core_config,
)
from .errors import ConfigError, CoreError, ProtocolError
from .interpretation import interpret_protocol_sample, prepare_protocol_write
from .measurement import (
    PointScalar,
    PointValue,
    PointWrite,
    ProtocolSample,
    Quality,
    WritableScalar,
)
from .port import (
    ConnectionHealth,
    ProtocolFactoryPort,
    ProtocolPort,
    ProtocolWrite,
    ProtocolWriteResult,
)
from .session import DeviceSession, create_device_session

__all__ = [
    "ConfigError",
    "ConfigRevision",
    "ConfigRevisionConflict",
    "ConnectionEndpoint",
    "ConnectionHealth",
    "CoreConfigArtifact",
    "CoreError",
    "CoreConfigCodecPort",
    "CoreConfigDiff",
    "CoreConfigRepositoryPort",
    "CoreConfigService",
    "CoreConfigSnapshot",
    "CoreConfigUpdateResult",
    "DeviceConnection",
    "DeviceSession",
    "IndexDiff",
    "PointScalar",
    "PointValue",
    "PointWrite",
    "ProtocolError",
    "ProtocolFactoryPort",
    "ProtocolPort",
    "ProtocolSample",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "Quality",
    "WritableScalar",
    "StoredCoreConfig",
    "compute_core_config_diff",
    "create_device_session",
    "interpret_protocol_sample",
    "prepare_protocol_write",
    "validate_core_config",
]
