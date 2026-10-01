"""Wind Hub 跨 Server/Collector 的稳定基础契约。

本包不包含采集 Runtime、配置写入或进程编排，只提供协议探测与主动验证
可以共同复用的数据模型、接口和网络探测基础能力。
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
