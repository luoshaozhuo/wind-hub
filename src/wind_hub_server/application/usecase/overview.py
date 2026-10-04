"""Overview 聚合只读用例。

将 Runtime 状态、Task 聚合状态和当前配置现场身份收敛为一次读取，避免
wind-hub-admin 为总览页拼接多个底层接口。这里只做读模型聚合，不修改状态。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub_server.application.port.monitoring import MonitoringSnapshotPort
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_server.application.usecase.worker_tasks import CollectorTaskUseCase


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


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


class OverviewUseCase:
    """Overview 页的应用层 Read Model。"""

    def __init__(
        self,
        *,
        monitoring: MonitoringSnapshotPort,
        tasks: CollectorTaskUseCase,
        config: ConfigUseCase,
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
            runtime_running=bool(status.get("running")),
            runtime_state=(
                "degraded"
                if bool(status.get("degraded"))
                else "running"
                if bool(status.get("running"))
                else "stopped"
            ),
            workers_unavailable=_as_str_list(status.get("unavailable_workers")),
            device_count=_as_int(status.get("device_count")),
            devices_connected=_as_int(status.get("devices_connected")),
            devices_offline=max(
                0,
                _as_int(status.get("device_count"))
                - _as_int(status.get("devices_connected")),
            ),
            sink_count=_as_int(status.get("sink_count")),
            sinks_healthy=_as_int(status.get("sinks_healthy")),
            task_count=len(tasks),
            task_instances=sum(task.instance_count for task in tasks),
            task_instances_running=sum(task.running_instances for task in tasks),
            task_instances_failed=sum(task.failed_instances for task in tasks),
            points_collected=_as_int(status.get("points_collected")),
            points_routed=_as_int(status.get("points_routed")),
            points_dropped=_as_int(status.get("points_dropped")),
        )
