"""Wind Hub 跨进程共享核心。

本包承载 Collector、Commander、Server 与运维客户端可共同依赖的稳定领域模型、
静态配置语义、RPC 契约、主动验证模型、网络探测和短生命周期协议验证能力。

本包不包含 Collector Runtime、任务调度、Sink、配置写回、Web API 或进程编排，
并禁止反向依赖任何可执行组件。
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
