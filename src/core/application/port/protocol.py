"""共享设备协议 outbound port。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.domain import PointTable, ProtocolPoint

from ..config import DeviceConnection
from ..measurement import ProtocolSample, WritableScalar


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    """协议连接的轻量缓存状态。"""

    healthy: bool
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ProtocolWrite:
    """一次协议点写入请求。"""

    point: ProtocolPoint
    value: WritableScalar


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
    """Collector 与 Commander 共享的最小设备通信能力边界。

    本接口只表达一次连接上的基础通信能力，不包含轮询调度、订阅生命周期、
    总召、重连退避等具体应用运行策略。
    """

    async def connect(self) -> None:
        """建立底层协议连接。"""
        ...

    async def close(self) -> None:
        """关闭底层协议连接；实现必须支持幂等调用。"""
        ...

    def health(self) -> ConnectionHealth:
        """返回缓存连接状态；不得因读取 health 触发网络 I/O。"""
        ...

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        """批量读取协议点。"""
        ...

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """批量写入协议点。"""
        ...


class ProtocolFactoryPort(Protocol):
    """按共享连接配置创建协议 Adapter 的工厂端口。"""

    def create(
        self,
        connection: DeviceConnection,
        point_table: PointTable,
    ) -> ProtocolPort:
        """创建尚未建立连接的协议实例。

        point_table 是已经 resolve 完成的共享点表。factory 必须是纯静态构造：
        可以解析连接参数、预编译 Symbol/IOA/寄存器映射，但不得执行网络 I/O。
        """
        ...



class SubscriptionHandle(Protocol):
    """一次协议订阅的生命周期句柄。"""

    async def close(self) -> None:
        """注销本次订阅；重复调用必须安全。"""
        ...


@runtime_checkable
class SubscribableProtocolPort(Protocol):
    """可选协议能力：接收设备主动上送/通知样本。

    本接口只表达协议能力，不规定 Collector 如何创建 Task、何时订阅或如何
    重订阅。callback 在实现所属 asyncio loop 中执行。
    """

    async def subscribe(
        self,
        points: Sequence[ProtocolPoint],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """注册一组协议点的样本回调。

        interval 是协议订阅自身的采样/通知周期提示；对天然事件驱动协议可忽略。
        它不表示 Collector 的 Task 调度策略。
        """
        ...


@runtime_checkable
class InterrogationCapableProtocolPort(Protocol):
    """可选协议能力：显式触发一次站级总召/全量刷新。"""

    async def interrogate(self) -> None:
        """触发一次协议定义的全量刷新操作。"""
        ...
