"""设备协议 outbound port。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from core.domain import ProtocolPoint

from ..measurement import PointScalar, ProtocolSample


class AcquisitionMode(StrEnum):
    """持续采集模式。"""

    POLL = "poll"
    SUBSCRIBE = "subscribe"


@dataclass(frozen=True, slots=True)
class ProtocolWrite:
    """一次协议点写入请求。"""

    point: ProtocolPoint
    value: PointScalar


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


class ProtocolPort(Protocol):
    """Application 依赖的最小设备协议能力边界。

    具体 ADS / Modbus / IEC104 Adapter 负责协议寻址、编码和网络 I/O；
    Application 只传递 ProtocolPoint 与标量值，不依赖第三方协议对象。
    """

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """声明该协议实例当前采用的持续采集模式。"""
        ...

    async def connect(self) -> None:
        """建立底层协议连接。"""
        ...

    async def close(self) -> None:
        """释放连接与独占资源；实现必须支持幂等关闭。"""
        ...

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        """批量读取协议点。

        Args:
            points: 待读取的协议点定义。

        Returns:
            每个返回值通过 point_id 与输入点关联。
        """
        ...

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """批量写入协议点。"""
        ...


class SubscriptionHandle(Protocol):
    """一次独立协议订阅的生命周期句柄。"""

    async def close(self) -> None:
        """注销本次订阅；实现必须支持幂等关闭。"""
        ...


@runtime_checkable
class SubscribableProtocolPort(Protocol):
    """可选能力：协议支持设备侧推送或订阅采集。"""

    async def subscribe(
        self,
        points: Sequence[ProtocolPoint],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """建立独立订阅并返回其生命周期句柄。"""
        ...
