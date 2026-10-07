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
from .interpretation import interpret_protocol_sample
from .measurement import PointScalar, PointValue, ProtocolSample, Quality
from .port import (
    ProtocolFactoryPort,
    ProtocolPort,
    ProtocolWrite,
    ProtocolWriteResult,
)
from .session import DeviceSession, create_device_session

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
    "DeviceConnection",
    "DeviceSession",
    "IndexDiff",
    "PointScalar",
    "PointValue",
    "ProtocolFactoryPort",
    "ProtocolPort",
    "ProtocolSample",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "Quality",
    "StoredCoreConfig",
    "compute_core_config_diff",
    "create_device_session",
    "interpret_protocol_sample",
    "validate_core_config",
]
