"""Domain 扩展点端口——由 domain 服务直接消费、由适配器实现的接口。

``ProtocolPort`` 是采集与运行时设备使用的协议扩展点，因此保留在 domain
层。Application 层自己的 outbound port（当前为 Sink）位于
``wind_hub.application.port``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.point import PointRef, PointValue

if TYPE_CHECKING:
    from wind_hub.config.schema import PointConfig


class AcquisitionMode(str, Enum):
    """协议驱动的持续采集能力——由驱动声明，与 Task 配置无关。

    - ``POLL``：协议是请求/响应式（或配置为主动读取），由调用方按
      Task interval 主动发起批量读；
    - ``SUBSCRIBE``：协议支持设备侧推送（ADS Device Notification、
      IEC104 spontaneous/periodic/interrogation），数据到达即回调。
    """

    POLL = "poll"
    SUBSCRIBE = "subscribe"


class SubscriptionHandle(Protocol):
    """一次订阅的句柄——关闭即注销本次订阅，不影响同设备的其他订阅。"""

    async def close(self) -> None:
        """注销订阅并释放其独占资源（必须幂等）。"""
        ...


@runtime_checkable
class InterrogationCapable(Protocol):
    """可选能力：master 侧总召（General Interrogation）。

    仅 IEC104 这类主站协议实现；``Device.start_acquisition`` 在订阅建立
    后探测本能力并触发一次总召，使总召响应经既有订阅链路上报。
    """

    async def interrogate(self) -> None:
        """发送一次 General Interrogation（C_IC_NA_1，QOI=20）。"""
        ...


class HealthStatus(BaseModel):
    """协议或 Sink 的轻量健康状态快照。"""

    healthy: bool
    """组件当前可正常工作时为 True。"""

    message: str | None = None
    """可选状态说明或错误摘要。"""


class ProtocolPort(Protocol):
    """设备协议扩展点。

    具体 Driver 负责 ADS、Modbus、IEC104 等 wire protocol；Runtime/Device 只依赖
    本接口，不直接依赖第三方协议库。
    """

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """注入设备完整点表并建立内部寻址映射。

        Args:
            points: 当前设备 resolved PointConfig 列表。

        Notes:
            该操作同步且只修改内存，不执行网络 I/O；无点表概念的 Driver 可 no-op。
        """
        ...

    async def connect(self) -> None:
        """建立底层协议连接。

        Raises:
            ProtocolError: 连接或协议握手失败。
        """
        ...

    async def close(self) -> None:
        """释放协议连接和其独占后台资源；必须支持幂等调用。"""
        ...

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """批量读取点位。

        Args:
            points: 待读取 PointRef。

        Returns:
            与输入顺序一致的 PointValue 列表。

        Raises:
            ProtocolError: 连接/传输级读取失败。
        """
        ...

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """批量写入命令。

        Args:
            cmds: 待执行 Command。

        Returns:
            与输入顺序一致的 CommandResult 列表。

        Raises:
            ProtocolError: 连接或传输级失败。
        """
        ...

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """本驱动的持续采集能力（``POLL`` / ``SUBSCRIBE``）。

        这是协议 capability，不是 Task 配置：Modbus 恒为 ``POLL``；
        ADS 由 ``subscribe_enabled`` 决定；IEC104 恒为 ``SUBSCRIBE``。
        """
        ...

    async def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """订阅指定点的推送更新。

        每次调用创建独立订阅；多个 Task Instance 可以订阅同一点而互不覆盖。

        Args:
            points: 订阅点列表。
            callback: 每个 PointValue 的异步回调。
            interval: 可选设备侧周期，例如 ADS notification cycle_time；IEC104
                等由远端决定时序的协议可忽略。

        Returns:
            仅管理本次订阅的 SubscriptionHandle。

        Raises:
            NotImplementedError: Driver 不支持订阅。
        """
        ...

    def health(self) -> HealthStatus:
        """返回缓存的连接健康状态；该同步接口不得主动探测 wire。"""
        ...
