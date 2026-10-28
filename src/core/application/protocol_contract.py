"""共享设备协议数据契约。

这里仅定义 ProtocolPort 与协议 Adapter 之间传递的稳定数据，不承载
Collector 采集结果模型或 Commander 命令用例模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, Literal, Never, Protocol, TypeAlias

from typing_extensions import TypeVar

from core.domain.sample_value import PointScalar, Quality, WritableScalar


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    """一次协议或 Sink 健康检查的结果。"""

    healthy: bool
    message: str | None = None


class ProtocolPoint(Protocol):
    """Minimal shared identity interface for protocol-native address objects."""

    @property
    def point_id(self) -> str: ...


TPoint = TypeVar("TPoint", bound=ProtocolPoint, covariant=True, default=Never)


@dataclass(frozen=True, slots=True)
class ProtocolWrite(Generic[TPoint]):
    """一次协议点写入请求。"""

    point: str
    value: WritableScalar

    def __post_init__(self) -> None:
        if not isinstance(self.point, str) or not self.point.strip():
            raise ValueError("point must be a configured point key")
        object.__setattr__(self, "point", self.point.strip())


class WriteOutcome(StrEnum):
    """一次写入的现场执行结果分类。

    - ``CONFIRMED_SUCCESS``：满足该点配置的协议确认条件，不等于物理到位；
    - ``CONFIRMED_FAILURE``：请求未发送（本地校验失败）或设备明确
      拒绝（异常响应/否定确认）——可以确证未执行；
    - ``UNKNOWN``：命令可能已送达或已执行，但没有足够证据确认最终
      结果（响应超时、确认丢失、写入期间连接中断）。UNKNOWN 结果
      不得被调用方盲目重试。
    """

    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProtocolWriteResult:
    """一次协议点写入结果。"""

    point_id: str
    outcome: WriteOutcome
    message: str | None = None

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        object.__setattr__(self, "point_id", point_id)


@dataclass(frozen=True, slots=True)
class InterrogationRequest:
    """IEC104 站/组召唤或计数器只读召唤。"""

    kind: Literal["station", "counter"] = "station"
    common_address: int | None = None
    group: int | None = None

    def __post_init__(self) -> None:
        max_group = 16 if self.kind == "station" else 4
        if self.kind not in ("station", "counter"):
            raise ValueError("unknown interrogation kind")
        if self.group is not None and (
            isinstance(self.group, bool) or not 1 <= self.group <= max_group
        ):
            raise ValueError(f"interrogation group must be in 1..{max_group}")
        if self.common_address is not None and (
            isinstance(self.common_address, bool) or not 0 <= self.common_address <= 65535
        ):
            raise ValueError("common address must be in 0..65535")


class ProtocolCapability(StrEnum):
    """统一协议 Port 可声明的运行时能力。"""

    READ = "read"
    READ_MANY = "read_many"
    WRITE = "write"
    # 多逻辑点批量写；未声明时 write_many 必须在连接恢复前明确拒绝。
    WRITE_MANY = "write_many"
    SUBSCRIBE = "subscribe"
    LISTEN = "listen"
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
    # Driver 在协议接收边界记录的 UTC 时刻，与设备采样时刻独立。
    receive_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    device_time: datetime | None = None
    # 协议原生诊断信息。不可变条目避免跨协程修改同一样本。
    protocol_metadata: tuple[tuple[str, int | str], ...] = ()

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("protocol sample timestamp must be timezone-aware")
        if self.receive_time.tzinfo is None:
            raise ValueError("protocol sample receive_time must be timezone-aware")
        if self.device_time is not None and self.device_time.tzinfo is None:
            raise ValueError("protocol sample device_time must be timezone-aware")
        if self.device_time is None and self.timestamp_source == "device":
            object.__setattr__(self, "device_time", self.timestamp)
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


__all__ = [
    "PointScalar",
    "WritableScalar",
    "Quality",
    "TimestampSource",
    "ConnectionHealth",
    "ProtocolPoint",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "ProtocolCapability",
    "ProtocolSample",
    "InterrogationRequest",
    "ProtocolLifecycleState",
    "ProtocolOperation",
    "ProtocolOperationState",
    "ProtocolOperationEvent",
    "ProtocolRuntimeState",
    "validate_read_many_results",
]
