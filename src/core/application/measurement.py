"""Application 层统一点值契约。

这里定义 Protocol Adapter 与上层应用之间传递的稳定值对象。Collector 与
Commander 可以共享这些通信值契约；它们不承担持久化或运行时生命周期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from core.domain import BusinessPointId, DeviceId, Unit

PointScalar: TypeAlias = float | int | bool | str | None


class Quality(StrEnum):
    """统一点值质量。"""

    GOOD = "good"
    BAD = "bad"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ProtocolSample:
    """协议 Adapter 返回的一条原始点值。

    timestamp 为 None 表示协议没有提供设备侧时间戳；Application 在归一化时
    必须显式补充采集时间，避免 Adapter 与领域模型各自隐式取当前时间。
    """

    point_id: str
    value: PointScalar
    quality: Quality = Quality.GOOD
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        object.__setattr__(self, "point_id", point_id)


@dataclass(frozen=True, slots=True)
class PointValue:
    """Application 完成映射与单位归一化后的标准点值。

    business_point_id 是稳定业务语义；unit 必须是对应 BusinessPoint 的
    standard_unit。source_point_id 保留协议点来源，便于诊断与追踪。
    """

    device_id: DeviceId
    business_point_id: BusinessPointId
    value: PointScalar
    unit: Unit
    quality: Quality
    timestamp: datetime
    source_point_id: str

    def __post_init__(self) -> None:
        device_id = self.device_id.strip()
        business_point_id = self.business_point_id.strip()
        source_point_id = self.source_point_id.strip()

        if not device_id:
            raise ValueError("device_id must not be empty")
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        if not source_point_id:
            raise ValueError("source_point_id must not be empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")

        object.__setattr__(self, "device_id", DeviceId(device_id))
        object.__setattr__(
            self,
            "business_point_id",
            BusinessPointId(business_point_id),
        )
        object.__setattr__(self, "source_point_id", source_point_id)
