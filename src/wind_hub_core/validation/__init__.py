"""配置主动验证共享契约；只导出探测数据模型，不包含验证编排策略。"""

from .models import (
    AddressResolution,
    DeviceProbeTarget,
    PointProbeSpec,
    ValidationCode,
    ValidationSeverity,
)

__all__ = [
    "AddressResolution",
    "DeviceProbeTarget",
    "PointProbeSpec",
    "ValidationCode",
    "ValidationSeverity",
]
