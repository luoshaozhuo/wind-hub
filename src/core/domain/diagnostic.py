"""可跨进程复用的诊断结果与协议地址模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DiagnosticCode(StrEnum):
    """诊断阶段的稳定结果码（gRPC/CLI 契约）。"""

    OK = "OK"
    PING_FAILED = "PING_FAILED"
    TCP_PORT_UNREACHABLE = "TCP_PORT_UNREACHABLE"
    PROTOCOL_CONNECT_FAILED = "PROTOCOL_CONNECT_FAILED"
    POINT_RESOLVE_FAILED = "POINT_RESOLVE_FAILED"
    POINT_READ_FAILED = "POINT_READ_FAILED"
    POINT_MAPPING_MISMATCH = "POINT_MAPPING_MISMATCH"


class DiagnosticSeverity(StrEnum):
    """诊断阶段严重级别。"""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class DiagnosticStage:
    """设备链路中的一个独立验证阶段。"""

    name: str
    ok: bool
    code: DiagnosticCode
    severity: DiagnosticSeverity
    message: str = ""


@dataclass(frozen=True, slots=True)
class DeviceVerifyResult:
    """设备通信链路分层验证结果。"""

    device_id: str
    protocol: str
    host: str
    port: int | None
    ok: bool
    stages: tuple[DiagnosticStage, ...] = ()


@dataclass(frozen=True, slots=True)
class PointVerifyResult:
    """单点在线验证结果，同时保留协议原始值与工程值。"""

    device_id: str
    point_id: str
    variable_name: str | None
    protocol: str
    configured_address: dict[str, Any]
    resolved_address: dict[str, Any] | None
    data_type: str
    scale: float
    offset: float
    unit: str
    readable: bool | None = None
    ok: bool = False
    code: DiagnosticCode = DiagnosticCode.OK
    severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    raw_value: Any = None
    engineering_value: Any = None
    quality: str | None = None
    source: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PointsVerifyResult:
    """一批点位的在线验证结果。"""

    device_id: str
    point_group: str | None
    checked: int
    passed: int
    failed: int
    ok: bool
    points: tuple[PointVerifyResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    """协议地址解析事实（ADS 为 symbol 绑定的 session 地址）。"""

    symbol: str | None
    index_group: int
    index_offset: int
    size: int | None
    protocol_type: str


