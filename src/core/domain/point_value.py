"""跨应用共享的不可变工程点值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.application.protocol_contract import PointScalar, Quality


@dataclass(frozen=True, slots=True)
class PointValue:
    """设备工程点值；时间戳统一为 UTC。"""

    device_id: str
    point_id: str
    value: PointScalar
    quality: Quality = Quality.GOOD
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str | None = None

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise ValueError("device_id must not be empty")
        if not self.point_id.strip():
            raise ValueError("point_id must not be empty")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
