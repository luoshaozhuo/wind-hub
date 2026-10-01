"""主动配置验证的数据契约。

这些模型只表达事实结果，不决定配置是否写回、何时 reload，也不持有
Collector Runtime 对象。Server 和 Collector 可共享同一结果语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ValidationSeverity(StrEnum):
    """验证结果严重度。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationCode(StrEnum):
    """主动验证的稳定错误/告警码。"""

    OK = "OK"
    PING_FAILED = "PING_FAILED"
    TCP_PORT_UNREACHABLE = "TCP_PORT_UNREACHABLE"
    PROTOCOL_CONNECT_FAILED = "PROTOCOL_CONNECT_FAILED"
    POINT_RESOLVE_FAILED = "POINT_RESOLVE_FAILED"
    POINT_MAPPING_MISMATCH = "POINT_MAPPING_MISMATCH"
    POINT_READ_FAILED = "POINT_READ_FAILED"


@dataclass(frozen=True, slots=True)
class DeviceProbeTarget:
    """协议探测所需的设备端点快照。

    options 只承载协议适配层需要的连接参数；共享层不解释其内容。
    """

    device_id: str
    protocol: str
    host: str
    port: int
    point_table: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PointProbeSpec:
    """协议无关的点探测输入。"""

    point_id: str
    data_type: str
    address: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AddressResolution:
    """变量名解析出的协议地址。

    ADS 使用 index_group / index_offset；其他协议可通过 extra 返回自己的地址元数据。
    """

    point_id: str
    symbol: str | None
    index_group: int | None = None
    index_offset: int | None = None
    size: int | None = None
    protocol_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PointValidationResult:
    """单点在单设备上的主动验证结果。"""

    point_id: str
    readable: bool
    code: ValidationCode
    severity: ValidationSeverity
    message: str = ""
    resolved: AddressResolution | None = None


@dataclass(frozen=True, slots=True)
class DeviceValidationReport:
    """单台设备的分层验证结果。"""

    device_id: str
    point_table: str
    ping_ok: bool
    port_ok: bool
    protocol_ok: bool
    code: ValidationCode
    severity: ValidationSeverity
    message: str = ""
    points: tuple[PointValidationResult, ...] = ()

    @property
    def ok(self) -> bool:
        """设备是否通过全部关键验证。"""
        return (
            self.ping_ok
            and self.port_ok
            and self.protocol_ok
            and all(point.readable for point in self.points)
        )
