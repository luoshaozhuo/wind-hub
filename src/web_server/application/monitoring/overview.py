"""Overview 聚合只读服务。

将 Runtime 状态、Task 聚合状态和当前配置现场身份收敛为一次读取，避免
wind-hub-admin 为总览页拼接多个底层接口。这里只做读模型聚合，不修改状态。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.port.monitoring import MonitoringSnapshotPort
from wind_hub_server.application.task.control import TaskControlService


class OverviewSnapshot(BaseModel):
    """管理总览页需要的核心运行快照。"""

    site_id: str | None = None
    site_name: str | None = None
    runtime_running: bool
    runtime_state: str
    workers_unavailable: list[str]
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


class OverviewService:
    """Overview 页的应用层 Read Model。"""

    def __init__(
        self,
        *,
        monitoring: MonitoringSnapshotPort,
        tasks: TaskControlService,
        config: ConfigService,
    ) -> None:
        self._monitoring = monitoring
        self._tasks = tasks
        self._config = config

    def snapshot(self) -> OverviewSnapshot:
        """返回一次一致的当前进程级总览快照。"""
        status = self._monitoring.runtime_status()
        tasks = self._tasks.list_task_summaries()
        site = self._config.current_config.system.site
        return OverviewSnapshot(
            site_id=site.site_id if site is not None else None,
            site_name=site.name if site is not None else None,
            runtime_running=status.running,
            runtime_state=(
                "degraded"
                if status.degraded
                else "running"
                if status.running
                else "stopped"
            ),
            workers_unavailable=list(status.unavailable_workers),
            device_count=status.device_count,
            devices_connected=status.devices_connected,
            devices_offline=max(
                0,
                status.device_count - status.devices_connected,
            ),
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
