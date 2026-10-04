"""Slow sink — 背压测试专用 SinkPort fixture，write 带可配置延迟。

只用于测试侧注入（component 层自定义 sink_factory），不进入生产装配：
用「写入慢」制造 queue 积压，验证 backpressure 策略与快慢 Sink 隔离。
"""

from __future__ import annotations

import asyncio

from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue


class SlowSink(SinkPort):
    """每次 write 睡眠 ``delay`` 秒后再落账的 Sink。"""

    def __init__(self, delay: float) -> None:
        if delay <= 0:
            raise ValueError("delay must be positive")
        self._delay = delay
        self.received: list[PointValue] = []
        self.completed_writes = 0

    async def open(self) -> None:
        """无外部资源可初始化。"""

    async def close(self) -> None:
        """无外部资源可释放。"""

    async def write(self, batch: list[PointValue]) -> None:
        await asyncio.sleep(self._delay)
        self.received.extend(batch)
        self.completed_writes += 1

    async def flush(self) -> None:
        """无缓冲数据。"""

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True)
