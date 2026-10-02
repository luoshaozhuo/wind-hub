"""Server Task assignment 读取模型。"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.usecase.config import ConfigUseCase


class TaskAssignment(BaseModel):
    """单个 Task 的 Collector 归属。"""

    task_id: str
    worker_id: str


class TaskAssignmentUseCase:
    """当前静态 placement：全部 Task 分配给默认 Collector。

    Task 集合来自 Server 当前成功配置，因此热重载成功后无需额外同步。
    后续可替换为持久化或调度器驱动的 placement 实现。
    """

    def __init__(
        self,
        config: ConfigUseCase,
        collectors: CollectorDirectory,
    ) -> None:
        self._config = config
        self._collectors = collectors

    def worker_for_task(self, task_id: str) -> str:
        """返回指定 Task 的 Collector；未知 Task 抛 KeyError。"""
        if not any(
            task.task_id == task_id
            for task in self._config.current_config.tasks.tasks
        ):
            raise KeyError(task_id)
        return self._collectors.default_worker_id

    def list_assignments(self) -> list[TaskAssignment]:
        """返回当前成功配置中全部 Task 的静态 assignment。"""
        worker_id = self._collectors.default_worker_id
        return [
            TaskAssignment(task_id=task.task_id, worker_id=worker_id)
            for task in self._config.current_config.tasks.tasks
        ]

    def task_ids_for_worker(self, worker_id: str) -> list[str]:
        """返回当前分配给指定 Worker 的 Task ID。"""
        self._collectors.get(worker_id)
        return [
            row.task_id
            for row in self.list_assignments()
            if row.worker_id == worker_id
        ]

    def worker_ids_for_device(self, device_id: str) -> list[str]:
        """返回实际承载指定设备采集 Task 的 Collector；无 Task 时回退默认 Worker。"""
        devices = self._config.current_config.devices.devices
        device = next((item for item in devices if item.device_id == device_id), None)
        if device is None:
            raise KeyError(device_id)

        assignments = {row.task_id: row.worker_id for row in self.list_assignments()}
        workers: set[str] = set()
        for task in self._config.current_config.tasks.tasks:
            if not task.enabled:
                continue
            matches = (
                task.device == device_id
                if task.device is not None
                else task.device_group == device.device_group
            )
            if matches:
                workers.add(assignments[task.task_id])
        return sorted(workers) or [self._collectors.default_worker_id]

    def worker_ids_for_sink(self, sink_name: str) -> list[str]:
        """返回实际向指定 Sink 写入的 Collector；无 Task 引用时回退默认 Worker。"""
        if not any(
            sink.name == sink_name
            for sink in self._config.current_config.system.sinks
        ):
            raise KeyError(sink_name)

        assignments = {row.task_id: row.worker_id for row in self.list_assignments()}
        workers = {
            assignments[task.task_id]
            for task in self._config.current_config.tasks.tasks
            if task.enabled and any(target.sink == sink_name for target in task.targets)
        }
        return sorted(workers) or [self._collectors.default_worker_id]
