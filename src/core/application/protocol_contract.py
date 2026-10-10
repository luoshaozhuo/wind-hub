"""共享设备协议数据契约。

这里仅定义 ProtocolPort 与协议 Adapter 之间传递的稳定数据，不承载
Collector 采集结果模型或 Commander 命令用例模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from core.domain.sample_value import PointScalar, Quality, WritableScalar


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



TimestampSource: TypeAlias = Literal["device", "local"]
"""样本时间戳来源。

- ``device``：协议原生时间戳（如 IEC104 CP56Time2a），表示对端设备的
  采样时刻；
- ``local``：协议不提供时间戳时，由 Driver 在获得读取结果的时刻生成的
  本地 UTC 时间（``datetime.now(UTC)``），不是设备采样时刻。
"""


@dataclass(frozen=True, slots=True)
class ProtocolSample:
    """协议 Adapter 返回的一条原始点值。

    ``timestamp`` 为必填且必须带时区（统一 UTC）：协议具备有效原生
    时间戳时优先使用（``timestamp_source="device"``），否则由 Driver
    在取得结果时生成 ``datetime.now(UTC)``（``"local"``）。BAD 质量
    样本同样必须给出明确时间戳。
    """

    point_id: str
    value: PointScalar
    timestamp: datetime
    quality: Quality = Quality.GOOD
    timestamp_source: TimestampSource = "local"

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("protocol sample timestamp must be timezone-aware")
        object.__setattr__(self, "point_id", point_id)


def validate_read_many_results(
    point_ids: Sequence[str],
    samples: Sequence[ProtocolSample],
) -> None:
    """校验 read_many 结果满足批量契约：数量一致、逐位对应（含重复点）。

    任何缺失、乱序、错位或多余样本都属于协议边界违约——调用方必须
    显式失败，不允许静默丢点或把别的点的值错配到请求点上。
    """
    from .errors import ProtocolError  # 延迟导入避免循环依赖

    if len(samples) != len(point_ids):
        raise ProtocolError(
            f"read_many returned {len(samples)} sample(s) for "
            f"{len(point_ids)} requested point(s)"
        )
    for index, (point_id, sample) in enumerate(zip(point_ids, samples, strict=True)):
        if sample.point_id != point_id:
            raise ProtocolError(
                f"read_many result mismatch at position {index}: "
                f"requested '{point_id}', got '{sample.point_id}'"
            )
