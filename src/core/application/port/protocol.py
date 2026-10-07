"""共享设备协议 outbound ports。

基础 Port 只定义 Collector 与 Commander 都依赖的最小通信能力；订阅、总召等
可选协议能力使用独立 capability Port 表达，避免基础接口要求所有协议实现。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from ..protocol_contract import (
    ConnectionHealth,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)


class ProtocolPort(Protocol):
    """共享的最小设备通信能力边界。"""

    async def connect(self) -> None:
        ...

    async def close(self) -> None:
        ...

    def health(self) -> ConnectionHealth:
        ...

    async def read(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        ...

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        ...


class SubscriptionHandle(Protocol):
    """协议订阅生命周期句柄。"""

    async def close(self) -> None:
        ...


ProtocolSampleCallback = Callable[[ProtocolSample], Awaitable[None]]


@runtime_checkable
class SubscribableProtocolPort(ProtocolPort, Protocol):
    """支持主动上送或设备通知订阅的协议能力。"""

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        ...


@runtime_checkable
class InterrogatableProtocolPort(ProtocolPort, Protocol):
    """支持显式总召或等价全站召唤的协议能力。"""

    async def interrogate(self) -> None:
        ...
