"""共享设备协议 outbound port。

ProtocolPort 统一描述 Shared Core 支持的协议运行时能力集合。具体协议通过
capabilities() 声明实际支持项；调用未支持能力时由 Adapter 显式失败。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Protocol, TypeVar

from ..protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)

if TYPE_CHECKING:
    from core.domain import PointTable


class SubscriptionHandle(Protocol):
    """协议订阅生命周期句柄。"""

    async def close(self) -> None: ...


ProtocolSampleCallback = Callable[[ProtocolSample], Awaitable[None]]


TPoint = TypeVar("TPoint")


class ProtocolPort(Protocol[TPoint]):
    """统一设备协议运行时能力边界。"""

    def capabilities(self) -> frozenset[ProtocolCapability]: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def is_open(self) -> bool:
        """本地传输连接当前是否处于打开状态（纯本地查询，不执行网络 I/O）。

        只回答「连接能否直接用于下一次 I/O」，不回答「对端设备是否能
        正常应答」——逐次通信的成败由读写调用本身的异常/结果表达。
        """
        ...

    async def health(self) -> ConnectionHealth:
        """执行一次协议级健康检查。

        实现必须判断当前对端通信是否仍然有效；不得在本方法内自动重连、
        恢复订阅或重发业务写入。调用方负责施加整体超时。
        """
        ...

    async def read_one(self, point_id: str | TPoint) -> ProtocolSample: ...

    async def read_many(
        self,
        point_ids: Sequence[str | TPoint],
    ) -> tuple[ProtocolSample, ...]: ...

    async def write_one(self, write: ProtocolWrite[TPoint]) -> ProtocolWriteResult: ...

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite[TPoint]],
    ) -> tuple[ProtocolWriteResult, ...]: ...

    def update_point_table(self, point_table: PointTable) -> None:
        """用热重载后的点表重建协议寻址映射（纯内存操作，不断开连接）。

        点表内容（地址/数据类型/字序）或绑定变化时由会话层调用。实现方
        必须同步失效依赖旧寻址的缓存（读取分组、地址解析等）；订阅类
        Driver 的已注册通知由上层重启订阅后按新映射重建。
        """
        ...

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle: ...

    async def interrogate(self) -> None: ...
