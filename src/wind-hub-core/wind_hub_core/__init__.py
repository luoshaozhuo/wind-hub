"""Wind Hub 跨进程共享的稳定基础契约。

本包只承载 wind-hub-ctl、wind-hub-collector 与 wind-hub-server 可共同依赖的
RPC 方法名、主动验证模型、网络探测和短生命周期协议验证能力。

本包不包含 Collector Runtime、任务调度、配置写回、Web API 或进程编排，避免
共享层反向依赖任一可执行组件。
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
