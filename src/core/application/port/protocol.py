"""共享设备协议 outbound port。

ProtocolPort 统一描述 Shared Core 支持的协议运行时能力集合。具体协议通过
capabilities() 声明**真实具备**的能力；调用未支持能力时由 Adapter 抛出
``ProtocolCapabilityError``，不允许以轮询、缓存或其他协议操作伪装。

两类推送语义严格区分：

- ``subscribe``：协议**原生远程订阅**（如 ADS AddDeviceNotification），
  调用会在对端设备注册通知；
- ``listen``：**本地监听**设备主动上送（如 IEC104 自发上送/总召数据），
  只做本地注册与分发，不向对端注册、不轮询、不隐式总召。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Never, Protocol

from typing_extensions import TypeVar

from ..protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolPoint,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)

if TYPE_CHECKING:
    from core.domain import PointTable


class SubscriptionHandle(Protocol):
    """一次协议原生远程订阅的生命周期句柄。"""

    async def close(self) -> None: ...


class ListenHandle(Protocol):
    """一次本地主动上送监听的生命周期句柄。"""

    async def close(self) -> None: ...


# Push 的交付单元是不可变、非空的协议原生批次：ADS Notification Block
# 或 IEC104 一次 APDU 解出的有效监视点，调用方不可假定每次只有一个点。
ProtocolSampleCallback = Callable[[tuple[ProtocolSample, ...]], Awaitable[None]]

TPoint = TypeVar("TPoint", bound=ProtocolPoint, contravariant=True, default=Never)


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

    async def read_one(self, point_id: str) -> ProtocolSample: ...

    async def read_many(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]: ...

    async def write_one(self, write: ProtocolWrite[TPoint]) -> ProtocolWriteResult: ...

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite[TPoint]],
    ) -> tuple[ProtocolWriteResult, ...]: ...

    def update_point_table(self, point_table: PointTable) -> None:
        """用热重载后的点表重建协议寻址映射（纯内存操作，不断开连接）。

        点表内容（地址/数据类型/字序）或绑定变化时由会话层调用。实现方
        必须同步失效依赖旧寻址的缓存（读取分组、地址解析等）；推送类
        Driver 的句柄由上层重启采集后按新映射重建。
        """
        ...

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """注册协议**原生远程订阅**（对端设备真实支持通知机制时）。

        空 ``point_ids`` 表示订阅点表全部点。实现必须返回句柄；注销统一
        通过 ``handle.close()``（幂等）完成，注销后不得继续投递新数据。
        """
        ...

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        """注册本地监听：接收设备**主动上送**的数据。

        本调用不向对端注册任何东西、不触发轮询或隐式总召、不创建额外
        连接。空 ``point_ids`` 表示监听点表内全部已配置点；未配置地址的
        上送不得投递到业务回调。注销统一通过 ``handle.close()``（幂等），
        注销后不再投递。
        """
        ...

    async def interrogate(
        self, common_address: int | None = None, *, qoi: int = 20
    ) -> tuple[ProtocolSample, ...]:
        """执行一次总召/组召，返回本次召唤的数据集合。

        ``common_address=None`` 表示全部已配置站址；QOI 20 为站总召，
        21..36 为组召。以 ACT_TERM 为完成依据；Listener 仍持续接收。
        """
        ...
