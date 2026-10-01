"""配置主动验证共享契约。"""

from .models import (
    AddressResolution,
    DeviceProbeTarget,
    DeviceValidationReport,
    PointProbeSpec,
    PointValidationResult,
    ValidationCode,
    ValidationSeverity,
)
from .ports import ProtocolProbe

__all__ = [
    "AddressResolution",
    "DeviceProbeTarget",
    "DeviceValidationReport",
    "PointProbeSpec",
    "PointValidationResult",
    "ProtocolProbe",
    "ValidationCode",
    "ValidationSeverity",
]
