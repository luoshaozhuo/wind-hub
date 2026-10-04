"""Sink outbound port——把点值批次投递到外部系统的能力边界。

由 application 层（Runtime 的投递编排）消费；Kafka / 文件 / 数据库等
实现位于 ``adapter.outbound.sink``，由组合根装配注入。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue


class SinkPort(Protocol):
    """点值批量交付的 outbound port。

    实现可以是文件、Kafka、数据库等外部介质。Sink 只接收 Runtime 已路由好的
    PointValue，不感知 Task Definition、设备协议或控制面。
    """

    async def open(self) -> None:
        """初始化外部 Sink 资源。

        Raises:
            SinkError: 文件、连接或外部资源初始化失败。
        """
        ...

    async def close(self) -> None:
        """释放 Sink 资源。

        必须支持幂等调用；实现应在关闭前处理自己的缓存和连接。
        """
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """写入一批 PointValue。

        Args:
            batch: Runtime 已决定路由到该 Sink 的点值。

        Raises:
            SinkError: 本批交付失败。
        """
        ...

    async def flush(self) -> None:
        """强制把实现内部缓冲提交到底层介质。

        优雅停机阶段会调用该方法，以尽量降低已进入 Sink 的数据丢失风险。

        Raises:
            SinkError: flush 失败。
        """
        ...

    def health(self) -> HealthStatus:
        """返回缓存的 Sink 健康状态；不得在该同步接口中执行阻塞 I/O。"""
        ...


@runtime_checkable
class ExclusiveOpenSinkPort(Protocol):
    """可选 Sink 能力：重建时必须先关闭旧实例才能打开新实例。

    典型场景是监听固定 TCP 端口的 server 型 Sink。Runtime 仅在实现该能力
    且 exclusive_open 为 True 时采用 close-first 替换策略。
    """

    @property
    def exclusive_open(self) -> bool:
        """是否要求同名 Sink 重建采用 close-first。"""
        ...
