"""Null sink — 测试用最小 SinkPort fixture，只记录收到的数据，不写外部。

实现 ``collector.application.sink_port.SinkPort`` 的结构化契约，把收到的
``collector.domain.point_value.PointValue`` 存入内存，用于隔离 Runtime /
routing / lifecycle 测试：这些测试只需要一个可注入的 SinkPort 来断言
「采集 → 路由 → 输出」链路数据流向，不依赖任何生产 Sink 的外部资源。
本 fixture 不代表生产 Sink（File/Kafka/DB/IEC104/Modbus）的实现状态。
"""

from __future__ import annotations

from collector.domain.point_value import PointValue
from core.application import ConnectionHealth


class NullSink:
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

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)
