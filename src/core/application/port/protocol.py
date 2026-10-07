"""共享设备协议 outbound port。

ProtocolPort 统一描述 Shared Core 支持的协议运行时能力集合。具体协议通过
capabilities() 声明实际支持项；调用未支持能力时由 Adapter 显式失败。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from ..protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)


class SubscriptionHandle(Protocol):
    """协议订阅生命周期句柄。"""

    async def close(self) -> None:
        ...


ProtocolSampleCallback = Callable[[ProtocolSample], Awaitable[None]]


class ProtocolPort(Protocol):
    """统一设备协议运行时能力边界。"""

    def capabilities(self) -> frozenset[ProtocolCapability]:
        ...

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

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        ...

    async def interrogate(self) -> None:
        ...
