"""Collector 内部统一流转的点值领域模型。

PointValue 是采集链路的汇合点：主动轮询与订阅推送最终都以
``PointValue`` 批次进入采集引擎，经路由派发到 Sink。value 为工程值
（Device 会话层已按点表 scale/offset 换算）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.application import PointScalar, Quality


@dataclass(frozen=True, slots=True)
class PointValue:
    """系统内统一流转的单个点值（不可变值对象）。

    Attributes:
        device_id: 产生该点值的设备标识。
        point_id: 设备点表内 point_id。
        value: 工程值；实际类型由业务点 data_type 约束。
        quality: 统一数据质量。
        timestamp: 采集或生成时间（UTC）。
        source: 产生该值的协议来源（如 ads / modbus / iec104）。
    """

    device_id: str
    point_id: str
    value: PointScalar
    quality: Quality = Quality.GOOD
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str | None = None
