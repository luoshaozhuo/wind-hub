"""跨应用共享的不可变工程点值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from .sample_value import PointScalar, Quality


@dataclass(frozen=True, slots=True)
class PointValue:
    """设备工程点值；时间戳统一为 UTC。"""

    device_id: str
    point_id: str
    value: PointScalar
    quality: Quality = Quality.GOOD
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    timestamp_source: Literal["device", "local"] = "local"
    source: str | None = None
    receive_time: datetime | None = None
    device_time: datetime | None = None

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise ValueError("device_id must not be empty")
        if not self.point_id.strip():
            raise ValueError("point_id must not be empty")
        if self.timestamp_source not in ("device", "local"):
            raise ValueError("timestamp_source must be device or local")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
        receive_time = self.receive_time if self.receive_time is not None else self.timestamp
        if receive_time.tzinfo is None or receive_time.utcoffset() is None:
            raise ValueError("receive_time must be timezone-aware")
        object.__setattr__(self, "receive_time", receive_time.astimezone(UTC))
        device_time = self.device_time
        if device_time is None and self.timestamp_source == "device":
            device_time = self.timestamp
        if device_time is not None:
            if device_time.tzinfo is None or device_time.utcoffset() is None:
                raise ValueError("device_time must be timezone-aware")
            object.__setattr__(self, "device_time", device_time.astimezone(UTC))
