"""基于 Collector gRPC 的 Task 控制用例。"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from pydantic import BaseModel

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.worker import CollectorPort
from wind_hub_server.application.usecase.task_assignment import TaskAssignmentUseCase


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

    def __init__(
        self,
        collectors: CollectorDirectory,
        assignments: TaskAssignmentUseCase,
    ) -> None:
        self._collectors = collectors
        self._assignments = assignments

    async def list_tasks(self) -> list[TaskDetail]:
        rows = await self._list_assigned_task_rows()
        return [
            TaskDetail.model_validate(
                {**row, "assigned_worker_id": worker_id}
            )
            for worker_id, row in rows
        ]

    async def list_task_summaries(self) -> list[TaskSummary]:
        rows = await self._list_assigned_task_rows()
        return [
            TaskSummary.model_validate(
                {**row, "assigned_worker_id": worker_id}
            )
            for worker_id, row in rows
        ]

    async def list_instances(self) -> list[TaskInstanceDetail]:
        assignments = self._assignments.list_assignments()
        by_worker: dict[str, set[str]] = {}
        for assignment in assignments:
            by_worker.setdefault(assignment.worker_id, set()).add(assignment.task_id)

        results = await asyncio.gather(
            *(
                self._collectors.get(worker_id).list_task_instances()
                for worker_id in sorted(by_worker)
            )
        )
        rows: list[TaskInstanceDetail] = []
        for worker_id, worker_rows in zip(sorted(by_worker), results, strict=True):
            assigned_task_ids = by_worker[worker_id]
            rows.extend(
                TaskInstanceDetail.model_validate(
                    {**row, "assigned_worker_id": worker_id}
                )
                for row in worker_rows
                if str(row.get("task_id") or "") in assigned_task_ids
            )
        return rows

    async def get_instance(self, instance_id: str) -> TaskInstanceDetail:
        rows = await self.list_instances()
        for row in rows:
            if row.instance_id == instance_id:
                return row
        raise KeyError(instance_id)

    async def get_task_summary(self, task_id: str) -> TaskSummary:
        """返回指定 Task 的当前聚合状态。"""
        worker_id = self._assignments.worker_for_task(task_id)
        rows = await self._collectors.get(worker_id).list_tasks()
        for row in rows:
            if str(row.get("task_id") or "") == task_id:
                return TaskSummary.model_validate(
                    {**row, "assigned_worker_id": worker_id}
                )
        raise KeyError(task_id)

    async def list_task_instances(self, task_id: str) -> list[TaskInstanceDetail]:
        """返回指定 Task 当前展开的全部实例。"""
        worker_id = self._assignments.worker_for_task(task_id)
        rows = await self._collectors.get(worker_id).list_task_instances()
        return [
            TaskInstanceDetail.model_validate(
                {**row, "assigned_worker_id": worker_id}
            )
            for row in rows
            if str(row.get("task_id") or "") == task_id
        ]

    async def start_instance(self, instance_id: str) -> TaskInstanceDetail:
        current = await self.get_instance(instance_id)
        collector = self._collectors.get(current.assigned_worker_id)
        data = await collector.start_task_instance(instance_id)
        return TaskInstanceDetail.model_validate(
            {**data, "assigned_worker_id": current.assigned_worker_id}
        )

    async def stop_instance(self, instance_id: str) -> TaskInstanceDetail:
        current = await self.get_instance(instance_id)
        collector = self._collectors.get(current.assigned_worker_id)
        data = await collector.stop_task_instance(instance_id)
        return TaskInstanceDetail.model_validate(
            {**data, "assigned_worker_id": current.assigned_worker_id}
        )

    async def start_task(self, task_id: str) -> TaskSummary:
        worker_id = self._assignments.worker_for_task(task_id)
        collector = self._collectors.get(worker_id)
        data = await collector.start_task(task_id)
        return TaskSummary.model_validate(
            {**data, "assigned_worker_id": worker_id}
        )

    async def stop_task(self, task_id: str) -> TaskSummary:
        worker_id = self._assignments.worker_for_task(task_id)
        collector = self._collectors.get(worker_id)
        data = await collector.stop_task(task_id)
        return TaskSummary.model_validate(
            {**data, "assigned_worker_id": worker_id}
        )

    async def _list_assigned_task_rows(
        self,
    ) -> list[tuple[str, dict[str, Any]]]:
        """按 assignment 查询各 Collector，并过滤非本 Worker Task。"""
        assignments = self._assignments.list_assignments()
        by_worker: dict[str, set[str]] = {}
        for assignment in assignments:
            by_worker.setdefault(assignment.worker_id, set()).add(assignment.task_id)

        worker_ids = sorted(by_worker)
        results = await asyncio.gather(
            *(
                self._collectors.get(worker_id).list_tasks()
                for worker_id in worker_ids
            )
        )
        rows: list[tuple[str, dict[str, Any]]] = []
        for worker_id, worker_rows in zip(worker_ids, results, strict=True):
            assigned_task_ids = by_worker[worker_id]
            rows.extend(
                (worker_id, row)
                for row in worker_rows
                if str(row.get("task_id") or "") in assigned_task_ids
            )
        return rows

    async def start_all_instances(self) -> TaskBatchResult:
        _, collector = self._default_collector()
        return TaskBatchResult.model_validate(await collector.start_all())

    async def stop_all_instances(self) -> TaskBatchResult:
        _, collector = self._default_collector()
        return TaskBatchResult.model_validate(await collector.stop_all())

    def _default_collector(self) -> tuple[str, CollectorPort]:
        """返回当前默认 Collector 的逻辑 ID 与出站端口。"""
        worker_id = self._collectors.default_worker_id
        return worker_id, self._collectors.get(worker_id)
