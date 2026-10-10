"""跨应用共享的工程点值模型。

PointValue 表示经设备会话完成工程量转换后的数据，不承载采集调度、
数据路由或任何 Sink 实现细节。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from core.application.protocol_contract import PointScalar, Quality


@dataclass(frozen=True, slots=True)
class PointValue:
    """不可变工程点值；时间戳必须使用 UTC 时区。"""

    device_id: str
    point_id: str
    value: PointScalar
    timestamp: datetime
    quality: Quality = Quality.GOOD
    source: str | None = None

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise ValueError("device_id must not be empty")
        if not self.point_id.strip():
            raise ValueError("point_id must not be empty")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.timestamp.utcoffset().total_seconds() != 0:
            raise ValueError("timestamp must use UTC")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
