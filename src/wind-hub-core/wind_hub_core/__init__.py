"""Wind Hub 跨进程共享的稳定基础契约。

本包不包含 Collector Runtime、Server 配置写入或进程编排，只提供双方共同
使用的 RPC 方法契约、主动验证数据模型、网络探测和协议验证基础能力。
"""

from wind_hub_core.validation.models import (
    AddressResolution,
    DeviceProbeTarget,
    DeviceValidationReport,
    PointProbeSpec,
    PointValidationResult,
    ValidationCode,
    ValidationSeverity,
)

__all__ = [
    "AddressResolution",
    "DeviceProbeTarget",
    "DeviceValidationReport",
    "PointProbeSpec",
    "PointValidationResult",
    "ValidationCode",
    "ValidationSeverity",
]
