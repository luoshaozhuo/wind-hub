"""Overview 聚合只读用例。

将 Runtime 状态、Task 聚合状态和当前配置现场身份收敛为一次读取，避免
wind-hub-admin 为总览页拼接多个底层接口。这里只做读模型聚合，不修改状态。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub.application.usecase.task import TaskUseCase


class OverviewSnapshot(BaseModel):
    """管理总览页需要的核心运行快照。"""

    site_id: str | None = None
    site_name: str | None = None
    runtime_running: bool
    device_count: int
    devices_connected: int
    devices_offline: int
    sink_count: int
    sinks_healthy: int
    task_count: int
    task_instances: int
    task_instances_running: int
    task_instances_failed: int
    points_collected: int
    points_routed: int
    points_dropped: int


class OverviewUseCase:
    """Overview 页的应用层 Read Model。"""

    def __init__(
        self, *, query: QueryUseCase, tasks: TaskUseCase, config: ConfigUseCase
    ) -> None:
        self._query = query
        self._tasks = tasks
        self._config = config

    async def snapshot(self) -> OverviewSnapshot:
        """返回一次一致的当前进程级总览快照。"""
        status = await self._query.status()
        tasks = await self._tasks.list_task_summaries()
        site = self._config.current_config.system.site
        return OverviewSnapshot(
            site_id=site.site_id if site is not None else None,
            site_name=site.name if site is not None else None,
            runtime_running=status.running,
            device_count=status.device_count,
            devices_connected=status.devices_connected,
            devices_offline=max(0, status.device_count - status.devices_connected),
            sink_count=status.sink_count,
            sinks_healthy=status.sinks_healthy,
            task_count=len(tasks),
            task_instances=sum(task.instance_count for task in tasks),
            task_instances_running=sum(task.running_instances for task in tasks),
            task_instances_failed=sum(task.failed_instances for task in tasks),
            points_collected=status.points_collected,
            points_routed=status.points_routed,
            points_dropped=status.points_dropped,
        )
