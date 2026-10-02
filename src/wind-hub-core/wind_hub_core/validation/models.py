"""主动配置验证的数据契约。

这些模型只表达探测输入和事实结果，不决定配置是否写回、何时 reload，也不
持有 Collector Runtime 或协议 connection。

options/address 使用 dict[str, Any] 是因为协议扩展字段来自 YAML 动态边界；
具体字段语义由对应协议探测器收敛解释，不应把 Any 继续传入领域运行时。
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

    Attributes:
        device_id: 设备稳定标识。
        host: 目标主机地址。
        options: 协议扩展连接参数。Any 仅对应 YAML 动态字段，由协议探测器解释。
    """

    device_id: str
    host: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PointProbeSpec:
    """协议无关的点探测输入。

    Attributes:
        point_id: 点稳定标识。
        data_type: Wind Hub 点表数据类型。
        address: 协议地址字典。Any 仅对应 YAML 动态协议字段。
    """

    point_id: str
    data_type: str
    address: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AddressResolution:
    """变量名解析出的 ADS 地址事实。

    Attributes:
        point_id: 点稳定标识。
        symbol: PLC symbol；纯 index 地址点为 None。
        index_group: ADS index group。
        index_offset: ADS index offset。
        size: PLC 类型字节数；未知时为 None。
        protocol_type: PLC 返回的 symbol type 或配置类型。
    """

    point_id: str
    symbol: str | None
    index_group: int | None = None
    index_offset: int | None = None
    size: int | None = None
    protocol_type: str | None = None


@dataclass(frozen=True, slots=True)
class PointValidationResult:
    """单点在单设备上的主动验证结果。

    只记录探测事实和稳定错误码，不决定是否阻止 reload 或是否写回配置。

    Attributes:
        point_id: 点稳定标识。
        readable: 是否成功完成一次协议读取。
        code: 稳定验证结果码。
        severity: 结果严重度。
        message: 面向调用方的补充说明。
        resolved: 已解析协议地址；未解析或解析失败时为 None。
    """

    point_id: str
    readable: bool
    code: ValidationCode
    severity: ValidationSeverity
    message: str = ""
    resolved: AddressResolution | None = None


@dataclass(frozen=True, slots=True)
class DeviceValidationReport:
    """单台设备的分层验证结果。

    ping、TCP、协议连接和点读取分层保存，调用方可以区分网络阻断与协议错误。

    Attributes:
        device_id: 设备稳定标识。
        point_table: 本次验证使用的点表标识。
        ping_ok: ICMP 探测是否成功。
        port_ok: TCP 端口探测是否成功。
        protocol_ok: 协议级连接是否成功。
        code: 设备级稳定验证结果码。
        severity: 设备级结果严重度。
        message: 面向调用方的补充说明。
        points: 单点验证结果集合。
    """

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
        """判断设备是否通过全部关键验证。

        Returns:
            ping、TCP、协议连接均成功且所有点可读时为 True。
        """
        return (
            self.ping_ok
            and self.port_ok
            and self.protocol_ok
            and all(point.readable for point in self.points)
        )
