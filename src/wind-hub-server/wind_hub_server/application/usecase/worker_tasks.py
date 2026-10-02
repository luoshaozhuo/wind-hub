"""基于 Collector gRPC 的 Task 控制用例。"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from pydantic import BaseModel

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_server.application.usecase.task_assignment import (
    TaskAssignmentUseCase,
    TaskPlacementState,
)


class TaskInstanceState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class TaskDetail(BaseModel):
    task_id: str
    assigned_worker_id: str | None
    placement_state: TaskPlacementState
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
        config: ConfigUseCase,
    ) -> None:
        self._collectors = collectors
        self._assignments = assignments
        self._config = config

    async def list_tasks(self) -> list[TaskDetail]:
        """返回全部 Task Definition，包括未分配 Task。"""
        return [
            TaskDetail.model_validate(row.model_dump())
            for row in await self.list_task_summaries()
        ]

    async def list_task_summaries(self) -> list[TaskSummary]:
        """返回全部 Task 的 placement 与运行状态。"""
        runtime_rows = {
            str(row.get("task_id")): (worker_id, row)
            for worker_id, row in await self._list_assigned_task_rows()
        }
        rows: list[TaskSummary] = []
        for cfg in self._config.current_config.tasks.tasks:
            assignment = self._assignments.assignment_for_task(cfg.task_id)
            current = runtime_rows.get(cfg.task_id)
            if current is not None:
                worker_id, row = current
                rows.append(
                    TaskSummary.model_validate(
                        {
                            **row,
                            "assigned_worker_id": worker_id,
                            "placement_state": TaskPlacementState.ASSIGNED,
                        }
                    )
                )
                continue
            rows.append(self._fallback_summary(cfg, assignment.worker_id))
        return rows

    async def list_instances(self) -> list[TaskInstanceDetail]:
        assignments = [
            row
            for row in self._assignments.list_assignments()
            if row.worker_id is not None
        ]
        by_worker: dict[str, set[str]] = {}
        for assignment in assignments:
            assert assignment.worker_id is not None
            by_worker.setdefault(assignment.worker_id, set()).add(assignment.task_id)

        worker_ids = sorted(by_worker)
        results = await asyncio.gather(
            *(
                self._collectors.get(worker_id).list_task_instances()
                for worker_id in worker_ids
            )
        )
        rows: list[TaskInstanceDetail] = []
        for worker_id, worker_rows in zip(worker_ids, results, strict=True):
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
        """返回指定 Task placement 与当前聚合状态。"""
        cfg = next(
            (
                item
                for item in self._config.current_config.tasks.tasks
                if item.task_id == task_id
            ),
            None,
        )
        if cfg is None:
            raise KeyError(task_id)
        assignment = self._assignments.assignment_for_task(task_id)
        if assignment.worker_id is None:
            return self._fallback_summary(cfg, None)

        rows = await self._collectors.get(assignment.worker_id).list_tasks()
        for row in rows:
            if str(row.get("task_id") or "") == task_id:
                return TaskSummary.model_validate(
                    {
                        **row,
                        "assigned_worker_id": assignment.worker_id,
                        "placement_state": assignment.state,
                    }
                )
        return self._fallback_summary(cfg, assignment.worker_id)

    async def list_task_instances(self, task_id: str) -> list[TaskInstanceDetail]:
        """返回指定 Task 当前展开的全部实例；未分配 Task 返回空列表。"""
        assignment = self._assignments.assignment_for_task(task_id)
        if assignment.worker_id is None:
            return []
        rows = await self._collectors.get(assignment.worker_id).list_task_instances()
        return [
            TaskInstanceDetail.model_validate(
                {**row, "assigned_worker_id": assignment.worker_id}
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
            {
                **data,
                "assigned_worker_id": worker_id,
                "placement_state": TaskPlacementState.ASSIGNED,
            }
        )

    async def stop_task(self, task_id: str) -> TaskSummary:
        worker_id = self._assignments.worker_for_task(task_id)
        collector = self._collectors.get(worker_id)
        data = await collector.stop_task(task_id)
        return TaskSummary.model_validate(
            {
                **data,
                "assigned_worker_id": worker_id,
                "placement_state": TaskPlacementState.ASSIGNED,
            }
        )

    async def _list_assigned_task_rows(
        self,
    ) -> list[tuple[str, dict[str, Any]]]:
        """按 assignment 查询各 Collector，并过滤非本 Worker Task。"""
        assignments = [
            row
            for row in self._assignments.list_assignments()
            if row.worker_id is not None
        ]
        by_worker: dict[str, set[str]] = {}
        for assignment in assignments:
            assert assignment.worker_id is not None
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

    def _fallback_summary(
        self,
        cfg: Any,
        worker_id: str | None,
    ) -> TaskSummary:
        """构造未分配或 Worker 未返回 Task 时的诚实控制面状态。"""
        return TaskSummary(
            task_id=cfg.task_id,
            assigned_worker_id=worker_id,
            placement_state=(
                TaskPlacementState.ASSIGNED
                if worker_id is not None
                else TaskPlacementState.UNASSIGNED
            ),
            device=cfg.device,
            device_group=cfg.device_group,
            point_group=cfg.point_group,
            interval=cfg.interval,
            targets=[target.sink for target in cfg.targets],
            enabled=cfg.enabled,
            runtime_state="unavailable" if worker_id is not None else "unassigned",
            instance_count=0,
            running_instances=0,
            stopped_instances=0,
            failed_instances=0,
        )

    async def start_all_instances(self) -> TaskBatchResult:
        """按 assignment 启动全部 Task Instance。"""
        return await self._set_all_instances(start=True)

    async def stop_all_instances(self) -> TaskBatchResult:
        """按 assignment 停止全部 Task Instance。"""
        return await self._set_all_instances(start=False)

    async def _set_all_instances(self, *, start: bool) -> TaskBatchResult:
        """仅操作当前 assignment 覆盖的实例，并聚合批量结果。"""
        instances = await self.list_instances()
        target = TaskInstanceState.RUNNING if start else TaskInstanceState.STOPPED
        changed = [row for row in instances if row.state is not target]

        async def apply(row: TaskInstanceDetail) -> None:
            collector = self._collectors.get(row.assigned_worker_id)
            if start:
                await collector.start_task_instance(row.instance_id)
            else:
                await collector.stop_task_instance(row.instance_id)

        await asyncio.gather(*(apply(row) for row in changed))
        return TaskBatchResult(
            total=len(instances),
            changed=len(changed),
            unchanged=len(instances) - len(changed),
        )
