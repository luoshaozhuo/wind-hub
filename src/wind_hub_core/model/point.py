"""点引用、点值和统一数据质量领域模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Quality(str, Enum):
    """PointValue 的统一数据质量。"""

    GOOD = "good"
    """数据有效。"""
    BAD = "bad"
    """数据不可用或协议/设备明确报告异常。"""
    UNCERTAIN = "uncertain"
    """数据可用性存在不确定性，应谨慎使用。"""


class PointRef(BaseModel):
    """不含值的设备点引用，用于 read/subscribe 请求。"""

    device_id: str
    """设备稳定标识。"""

    point_id: str
    """设备点表内的 point_id。"""


class PointValue(BaseModel):
    """系统内统一流转的单个点值。

    ProtocolPort 产生 PointValue，Device 层应用 scale/offset，Runtime 再路由到
    Sink。value 使用 Any 是因为点表允许 float/int/bool/str 等多种标量；具体类型
    始终由 PointConfig.data_type 约束。
    """

    device_id: str
    """产生该点值的设备标识。"""

    point_id: str
    """设备点表内的 point_id。"""

    value: Any
    """点值本身；实际类型由点表 data_type 决定。"""

    quality: Quality = Quality.GOOD
    """点值质量。"""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """采集或生成时间，UTC。"""

    source: str | None = None
    """产生该值的协议来源，例如 ads、modbus。"""
