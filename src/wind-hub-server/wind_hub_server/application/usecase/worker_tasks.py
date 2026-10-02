"""基于 Collector gRPC 的 Task 控制用例。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from wind_hub_server.application.port.worker import CollectorPort


class TaskInstanceState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class TaskDetail(BaseModel):
    task_id: str
    assigned_worker_id: str
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float | None = None
    targets: list[str]
    enabled: bool


class TaskInstanceDetail(BaseModel):
    instance_id: str
    assigned_worker_id: str
    task_id: str
    device_id: str
    point_group: str
    interval: float | None = None
    targets: list[str]
    state: TaskInstanceState


class TaskBatchResult(BaseModel):
    total: int
    changed: int
    unchanged: int


class TaskSummary(TaskDetail):
    runtime_state: str
    instance_count: int
    running_instances: int
    stopped_instances: int
    failed_instances: int


class CollectorTaskUseCase:
    """Server 的 Task facade；所有生命周期操作发往独立 Collector。"""

    def __init__(self, collector: CollectorPort, *, worker_id: str) -> None:
        self._collector = collector
        self._worker_id = worker_id

    async def list_tasks(self) -> list[TaskDetail]:
        rows = await self._collector.list_tasks()
        return [
            TaskDetail.model_validate({**row, "assigned_worker_id": self._worker_id})
            for row in rows
        ]

    async def list_task_summaries(self) -> list[TaskSummary]:
        rows = await self._collector.list_tasks()
        return [
            TaskSummary.model_validate({**row, "assigned_worker_id": self._worker_id})
            for row in rows
        ]

    async def list_instances(self) -> list[TaskInstanceDetail]:
        rows = await self._collector.list_task_instances()
        return [
            TaskInstanceDetail.model_validate(
                {**row, "assigned_worker_id": self._worker_id}
            )
            for row in rows
        ]

    async def get_instance(self, instance_id: str) -> TaskInstanceDetail:
        rows = await self.list_instances()
        for row in rows:
            if row.instance_id == instance_id:
                return row
        raise KeyError(instance_id)

    async def get_task_summary(self, task_id: str) -> TaskSummary:
        """返回指定 Task 的当前聚合状态。"""
        rows = await self.list_task_summaries()
        for row in rows:
            if row.task_id == task_id:
                return row
        raise KeyError(task_id)

    async def list_task_instances(self, task_id: str) -> list[TaskInstanceDetail]:
        """返回指定 Task 当前展开的全部实例。"""
        summaries = await self.list_task_summaries()
        if not any(row.task_id == task_id for row in summaries):
            raise KeyError(task_id)
        return [row for row in await self.list_instances() if row.task_id == task_id]

    async def start_instance(self, instance_id: str) -> TaskInstanceDetail:
        data = await self._collector.start_task_instance(instance_id)
        return TaskInstanceDetail.model_validate(
            {**data, "assigned_worker_id": self._worker_id}
        )

    async def stop_instance(self, instance_id: str) -> TaskInstanceDetail:
        data = await self._collector.stop_task_instance(instance_id)
        return TaskInstanceDetail.model_validate(
            {**data, "assigned_worker_id": self._worker_id}
        )

    async def start_task(self, task_id: str) -> TaskSummary:
        data = await self._collector.start_task(task_id)
        return TaskSummary.model_validate(
            {**data, "assigned_worker_id": self._worker_id}
        )

    async def stop_task(self, task_id: str) -> TaskSummary:
        data = await self._collector.stop_task(task_id)
        return TaskSummary.model_validate(
            {**data, "assigned_worker_id": self._worker_id}
        )

    async def start_all_instances(self) -> TaskBatchResult:
        return TaskBatchResult.model_validate(await self._collector.start_all())

    async def stop_all_instances(self) -> TaskBatchResult:
        return TaskBatchResult.model_validate(await self._collector.stop_all())
