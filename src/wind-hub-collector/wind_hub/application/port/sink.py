"""Sink outbound port——把点值批次投递到外部系统的能力边界。

由 application 层（Runtime 的投递编排）消费；Kafka / 文件 / 数据库等
实现位于 ``adapter.outbound.sink``，由组合根装配注入。
"""

from __future__ import annotations

from typing import Protocol

from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus


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
