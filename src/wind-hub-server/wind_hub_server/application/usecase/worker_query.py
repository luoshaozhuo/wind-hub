"""基于 Collector/Commander RPC 的 Server 只读 Worker facade。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from wind_hub_core.model.point import PointValue
from wind_hub_server.application.port.worker import CollectorPort, CommanderPort


class AcquisitionInfo(BaseModel):
    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    running: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_duration: float | None = None


class SystemStatus(BaseModel):
    running: bool
    device_count: int
    sink_count: int
    devices_connected: int = 0
    sinks_healthy: int = 0
    points_collected: int = 0
    points_routed: int = 0
    points_dropped: int = 0
    acquisitions: list[AcquisitionInfo] = Field(default_factory=list)


class WorkerQueryUseCase:
    """Runtime 状态来自 Collector，即时点读取来自 Commander。"""

    def __init__(
        self,
        collector: CollectorPort,
        commander: CommanderPort,
    ) -> None:
        self._collector = collector
        self._commander = commander

    async def status(self) -> SystemStatus:
        return SystemStatus.model_validate(await self._collector.runtime_status())

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        return await self._commander.read_point(device_id, point_id)

    async def list_devices(self) -> list[dict[str, object]]:
        return list(await self._collector.list_devices())

    async def list_sinks(self) -> list[dict[str, object]]:
        return list(await self._collector.list_sinks())
