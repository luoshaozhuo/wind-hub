"""Sink outbound port——把点值批次投递到外部系统的能力边界。

由 application 层（Runtime 的投递编排）消费；Kafka / 文件 / 数据库等
实现位于 ``adapter.outbound.sink``，由组合根装配注入。
"""

from __future__ import annotations

from typing import Protocol

from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus


class SinkPort(Protocol):
    """Output extension point.

    Implementations persist or forward batches of ``PointValue``
    to external systems: Kafka topics, files, databases, etc.
    """

    async def open(self) -> None:
        """Initialise the sink (open files, connect to broker, …).

        Raises:
            SinkError: On initialisation failure.
        """
        ...

    async def close(self) -> None:
        """Finalise the sink (flush buffers, close connections).

        Must be safe to call even if already closed.
        """
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """Write a batch of point values.

        The batch contains only data that the router assigned to this
        sink — the sink does not need to know about routing rules.

        Args:
            batch: Point values to persist/forward.

        Raises:
            SinkError: On write failure.
        """
        ...

    async def flush(self) -> None:
        """Force-flush buffered data to the underlying medium.

        Used during graceful shutdown to ensure no data loss.

        Raises:
            SinkError: On flush failure.
        """
        ...

    def health(self) -> HealthStatus:
        """Return sink health status — synchronous, returns cached state."""
        ...
