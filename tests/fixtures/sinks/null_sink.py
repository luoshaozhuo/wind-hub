"""Null sink — 测试用 sink 替身，只记录收到的数据，不写外部。

生产 sink（File/Kafka/DB）的 ``write`` 仍是骨架（抛 ``NotImplementedError``），
端到端测试无法用它们断言数据落地。NullSink 实现
:class:`~wind_hub_collector.application.port.sink.SinkPort` 的最小契约，把收到的
:class:`~wind_hub_collector.domain.model.point.PointValue` 存入内存，供测试断言
「采集 → 路由 → 输出」链路是否真的把数据推到了 sink。
"""

from __future__ import annotations

from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.domain.model.point import PointValue
from wind_hub_core.model.health import HealthStatus


class NullSink(SinkPort):
    """只记录收到的数据，不做任何外部写入。"""

    def __init__(self) -> None:
        self.received: list[PointValue] = []

    async def open(self) -> None:
        """无外部资源可初始化。"""

    async def close(self) -> None:
        """无外部资源可释放。"""

    async def write(self, batch: list[PointValue]) -> None:
        """把整批点值追加到 ``received``。"""
        self.received.extend(batch)

    async def flush(self) -> None:
        """无缓冲数据。"""

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True)
