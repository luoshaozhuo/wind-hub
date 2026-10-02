"""Server Task placement 控制面状态。"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.usecase.config import ConfigUseCase


class TaskPlacementState(StrEnum):
    """Task 当前 placement 状态。"""

    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"


class TaskAssignment(BaseModel):
    """单个 Task 的 Collector placement。"""

    task_id: str
    worker_id: str | None
    state: TaskPlacementState


class TaskPlacementError(RuntimeError):
    """Task 当前没有可执行 placement。"""


class TaskAssignmentUseCase:
    """Server 持有的 Task placement 状态。

    初始启动和新增 Task 使用 rendezvous hashing 做稳定 placement；同一 Worker
    集合下新增/删除其他 Task 不会迁移已有 Task。显式迁移必须经 assign/unassign。
    """

    def __init__(
        self,
        config: ConfigUseCase,
        collectors: CollectorDirectory,
    ) -> None:
        self._config = config
        self._collectors = collectors
        self._placements: dict[str, str | None] = {}
        self._initialize()

    def _initialize(self) -> None:
        """为启动时 Task 建立稳定 placement。"""
        worker_ids = self._collectors.list_worker_ids()
        for task in self._config.current_config.tasks.tasks:
            self._placements[task.task_id] = self._select_worker(
                task.task_id,
                worker_ids,
            )

    def sync(self) -> None:
        """与当前成功配置同步 Task 集合，不迁移已有 Task。"""
        task_ids = {
            task.task_id
            for task in self._config.current_config.tasks.tasks
        }
        for task_id in list(self._placements):
            if task_id not in task_ids:
                del self._placements[task_id]

        existing_workers = set(self._collectors.list_worker_ids())
        for task_id, worker_id in list(self._placements.items()):
            if worker_id is not None and worker_id not in existing_workers:
                self._placements[task_id] = None

        worker_ids = sorted(existing_workers)
        for task_id in sorted(task_ids - set(self._placements)):
            self._placements[task_id] = self._select_worker(task_id, worker_ids)

    @staticmethod
    def _select_worker(task_id: str, worker_ids: list[str]) -> str | None:
        """用 rendezvous hashing 选择稳定 Worker；无 Worker 时返回 None。"""
        if not worker_ids:
            return None
        return max(
            worker_ids,
            key=lambda worker_id: sha256(
                f"{task_id}\0{worker_id}".encode("utf-8")
            ).digest(),
        )

    def assign(self, task_id: str, worker_id: str) -> TaskAssignment:
        """显式把 Task 分配给指定 Collector。"""
        self.sync()
        if task_id not in self._placements:
            raise KeyError(task_id)
        self._collectors.get(worker_id)
        self._placements[task_id] = worker_id
        return self.assignment_for_task(task_id)

    def unassign(self, task_id: str) -> TaskAssignment:
        """显式取消 Task placement。"""
        self.sync()
        if task_id not in self._placements:
            raise KeyError(task_id)
        self._placements[task_id] = None
        return self.assignment_for_task(task_id)

    def assignment_for_task(self, task_id: str) -> TaskAssignment:
        """返回指定 Task placement；未知 Task 抛 KeyError。"""
        self.sync()
        if task_id not in self._placements:
            raise KeyError(task_id)
        worker_id = self._placements[task_id]
        return TaskAssignment(
            task_id=task_id,
            worker_id=worker_id,
            state=(
                TaskPlacementState.ASSIGNED
                if worker_id is not None
                else TaskPlacementState.UNASSIGNED
            ),
        )

    def worker_for_task(self, task_id: str) -> str:
        """返回指定 Task 的 Collector；未分配 Task 抛 TaskPlacementError。"""
        assignment = self.assignment_for_task(task_id)
        if assignment.worker_id is None:
            raise TaskPlacementError(f"task '{task_id}' is unassigned")
        return assignment.worker_id

    def list_assignments(self) -> list[TaskAssignment]:
        """返回当前成功配置中全部 Task placement。"""
        self.sync()
        return [
            TaskAssignment(
                task_id=task_id,
                worker_id=worker_id,
                state=(
                    TaskPlacementState.ASSIGNED
                    if worker_id is not None
                    else TaskPlacementState.UNASSIGNED
                ),
            )
            for task_id, worker_id in sorted(self._placements.items())
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
        """返回实际承载指定设备采集 Task 的 Collector。"""
        devices = self._config.current_config.devices.devices
        device = next((item for item in devices if item.device_id == device_id), None)
        if device is None:
            raise KeyError(device_id)

        assignments = {
            row.task_id: row.worker_id
            for row in self.list_assignments()
            if row.worker_id is not None
        }
        workers: set[str] = set()
        for task in self._config.current_config.tasks.tasks:
            if not task.enabled or task.task_id not in assignments:
                continue
            matches = (
                task.device == device_id
                if task.device is not None
                else task.device_group == device.device_group
            )
            if matches:
                workers.add(assignments[task.task_id])
        return sorted(workers)

    def worker_ids_for_sink(self, sink_name: str) -> list[str]:
        """返回实际向指定 Sink 写入的 Collector。"""
        if not any(
            sink.name == sink_name
            for sink in self._config.current_config.system.sinks
        ):
            raise KeyError(sink_name)

        assignments = {
            row.task_id: row.worker_id
            for row in self.list_assignments()
            if row.worker_id is not None
        }
        workers = {
            assignments[task.task_id]
            for task in self._config.current_config.tasks.tasks
            if (
                task.enabled
                and task.task_id in assignments
                and any(target.sink == sink_name for target in task.targets)
            )
        }
        return sorted(workers)


__all__ = [
    "TaskAssignment",
    "TaskAssignmentUseCase",
    "TaskPlacementError",
    "TaskPlacementState",
]
