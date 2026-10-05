"""Server 聚合 Collector 低频运行快照端口。"""

from __future__ import annotations

from typing import Protocol

from wind_hub_server.application.port.monitoring import CollectorSnapshot


class CollectorQueryPort(Protocol):
    """MonitoringService 所需的一次性 Collector 聚合快照。"""

    async def snapshot(self) -> CollectorSnapshot: ...
