"""共享设备协议数据契约。

这里仅定义 ProtocolPort 与协议 Adapter 之间传递的稳定数据，不承载
Collector 采集结果模型或 Commander 命令用例模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

PointScalar: TypeAlias = float | int | bool | str | None
WritableScalar: TypeAlias = float | int | bool | str


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    """协议连接的轻量缓存状态。"""

    healthy: bool
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ProtocolWrite:
    """一次协议点写入请求。"""

    point_id: str
    value: WritableScalar

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        object.__setattr__(self, "point_id", point_id)


@dataclass(frozen=True, slots=True)
class ProtocolWriteResult:
    """一次协议点写入结果。"""

    point_id: str
    success: bool
    message: str | None = None

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        object.__setattr__(self, "point_id", point_id)


class ProtocolCapability(StrEnum):
    """统一协议 Port 可声明的运行时能力。"""

    READ = "read"
    WRITE = "write"
    # 多逻辑点批量写；未声明时 write_many 必须在连接恢复前明确拒绝。
    WRITE_MANY = "write_many"
    SUBSCRIBE = "subscribe"
    INTERROGATE = "interrogate"


class Quality(StrEnum):
    """统一协议点值质量。"""

    GOOD = "good"
    BAD = "bad"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ProtocolSample:
    """协议 Adapter 返回的一条原始点值。"""

    point_id: str
    value: PointScalar
    quality: Quality = Quality.GOOD
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            raise ValueError("protocol sample timestamp must be timezone-aware")
        object.__setattr__(self, "point_id", point_id)
