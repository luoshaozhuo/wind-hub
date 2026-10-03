"""Server 聚合 Collector 低频运行快照端口。"""

from __future__ import annotations

from typing import Any, Protocol


class CollectorQueryPort(Protocol):
    """MonitoringService 所需的一次性 Collector 聚合快照。"""

    async def snapshot(self) -> dict[str, Any]: ...
