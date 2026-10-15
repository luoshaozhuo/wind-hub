"""Server 读取与短期趋势使用的点值 DTO。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.domain.sample_value import PointScalar, Quality


@dataclass(frozen=True, slots=True)
class PointValue:
    device_id: str
    point_id: str
    value: PointScalar
    quality: Quality
    timestamp: datetime
    source: str | None = None
    timestamp_source: str = "local"

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("point timestamp must be timezone-aware")
