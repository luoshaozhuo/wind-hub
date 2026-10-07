"""共享设备协议 outbound ports。

本模块只定义 Application 所需接口。接口使用的数据契约定义在
core.application.measurement，具体实现位于 Infrastructure。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from core.domain import PointTable, ProtocolPoint

from ..config.device_connection import DeviceConnection
from ..measurement import (
    ConnectionHealth,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)


class ProtocolPort(Protocol):
    """Collector 与 Commander 共享的最小设备通信能力边界。"""

    async def connect(self) -> None:
        ...

    async def close(self) -> None:
        ...

    def health(self) -> ConnectionHealth:
        ...

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        ...

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        ...


class ProtocolFactoryPort(Protocol):
    """按共享连接配置创建协议 Adapter 的工厂边界。"""

    def create(
        self,
        connection: DeviceConnection,
        point_table: PointTable,
    ) -> ProtocolPort:
        ...


class SubscriptionHandle(Protocol):
    """一次协议订阅的生命周期接口。"""

    async def close(self) -> None:
        ...


@runtime_checkable
class SubscribableProtocolPort(Protocol):
    """可选协议能力：接收设备主动上送或通知样本。"""

    async def subscribe(
        self,
        points: Sequence[ProtocolPoint],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        ...


@runtime_checkable
class InterrogationCapableProtocolPort(Protocol):
    """可选协议能力：显式触发一次站级总召或全量刷新。"""

    async def interrogate(self) -> None:
        ...
