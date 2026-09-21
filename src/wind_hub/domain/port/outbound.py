"""Domain 扩展点端口——由 domain 服务直接消费、由适配器实现的接口。

``ProtocolPort`` 被 ``domain.acquisition`` / ``domain.command`` 消费，
``ProcessorPort`` 被 ``domain.processing`` 消费——它们是 domain 的扩展点，
因此保留在 domain 层。application 消费的端口（Sink / 调度）位于
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
    """Minimal health-check placeholder — refined in a later step."""

    healthy: bool
    """``True`` when the component is operating normally."""

    message: str | None = None
    """Optional human-readable status detail (e.g. error description)."""


class ProtocolPort(Protocol):
    """Protocol extension point.

    Implementations handle the wire protocol for a specific device
    family: ADS, Modbus, IEC104, etc.
    """

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """Inject the device's point table; the driver builds an
        ``address ↔ point_id`` mapping for later reads/writes.

        Pure in-memory and synchronous — no network I/O.  A driver
        that has no notion of a point table may implement this as a
        no-op.

        Args:
            points: The full point table for this device, from config.
        """
        ...

    async def connect(self) -> None:
        """Establish the underlying transport connection(s).

        Raises:
            ProtocolError: On connection failure.
        """
        ...

    async def close(self) -> None:
        """Tear down the transport connection(s).

        Must be safe to call even if already closed.
        """
        ...

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """Batch-read multiple points.

        Args:
            points: Points to read.

        Returns:
            One ``PointValue`` per input ``PointRef``, in the same order.

        Raises:
            ProtocolError: If any read fails.
        """
        ...

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """Batch-write multiple commands.

        Every input ``Command`` yields one ``CommandResult`` at the
        corresponding list index.

        Args:
            cmds: Commands to execute.

        Returns:
            One ``CommandResult`` per command, in input order.

        Raises:
            ProtocolError: On transport-level failure.
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
        """Subscribe to spontaneous updates for the given points.

        Each call creates an **independent** subscription: multiple callers
        may subscribe the same point with different intervals/callbacks
        without interfering with each other.  Closing the returned handle
        unregisters exactly this subscription.

        Args:
            points: Points to subscribe to.
            callback: Async callable invoked with each ``PointValue``.
            interval: Requested device-side cycle time in seconds
                (e.g. ADS ``NotificationAttrib.cycle_time``); ignored by
                protocols whose data timing is decided by the remote end
                (IEC104 spontaneous/periodic).

        Raises:
            NotImplementedError: If the protocol does not support
                subscription (e.g. drivers without subscription support).
        """
        ...

    def health(self) -> HealthStatus:
        """Return connection health status — synchronous by design.

        Callers poll this cheaply; the implementation returns cached
        state rather than probing the wire.
        """
        ...


class ProcessorPort(Protocol):
    """Processing extension point.

    Processors transform a batch of ``PointValue`` instances in place
    — filtering, enriching, or computing derived values.  They are
    chained together in the order specified by configuration.
    """

    @property
    def name(self) -> str:
        """Unique processor name used for logging and configuration."""
        ...

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        """Transform a batch of point values.

        The output list may differ in length from the input (filters
        drop values; derived-point processors add values).

        Args:
            batch: Input point values.

        Returns:
            Transformed point values.

        Raises:
            ProcessorError: On any transformation failure (e.g.
                external service enrichment fails).
        """
        ...


@runtime_checkable
class PointsConfigurable(Protocol):
    """An optional capability of a ``ProcessorPort`` implementation.

    Processors that transform values based on per-point configuration
    (scale/offset, deadband, min/max bounds) implement this to receive
    the full point table at assembly time, before any batch is processed.
    Processors without per-point config simply do not implement it; the
    composition root probes for this capability before injecting points.
    """

    def set_points_config(self, points_by_device: dict[str, list[PointConfig]]) -> None:
        """Inject the resolved point tables; build per-point lookup tables.

        Pure in-memory and synchronous — no I/O.  Called once by the
        composition root after the processor is created.

        Args:
            points_by_device: ``{device_id: [PointConfig, …]}``——设备绑定
                点表后的解析结果（同一表被多设备共享时，各设备键指向同一
                list 对象）。处理器通常据此建立 ``(device_id, point_id)``
                键的查找表。
        """
        ...
