"""Server 聚合 Collector 运行态查询端口。"""

from __future__ import annotations

from typing import Any, Protocol


class CollectorQueryPort(Protocol):
    """面向 Server 读模型的 Collector 聚合查询能力。"""

    async def runtime_status(self) -> dict[str, Any]: ...

    async def metrics_snapshot(self) -> dict[str, Any]: ...

    async def list_devices(self) -> list[dict[str, Any]]: ...

    async def list_sinks(self) -> list[dict[str, Any]]: ...
