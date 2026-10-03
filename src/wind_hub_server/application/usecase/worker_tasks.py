"""基于 Collector gRPC 的 Task 控制用例。"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from pydantic import BaseModel

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.worker import (
    CollectorPlacementRejectedError,
    CollectorPort,
)
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_server.application.usecase.task_assignment import (
    TaskAssignmentUseCase,
    TaskPlacementState,
)


class TaskWorkerUnavailableError(RuntimeError):
    """Task 所属 Collector 当前不可用或身份非法。"""


class TaskPlacementUnsafeError(RuntimeError):
    """当前 placement 尚未完成安全收敛，禁止新的 start 操作。"""


class TaskInstanceState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


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


class TaskPlacementReconcileResult(BaseModel):
    """一次 placement reconciliation 结果。"""

    safe: bool
    generation: int
    scanned_workers: int
    unavailable_workers: list[str]
    orphaned_tasks: list[str]
    examined_instances: int
    wrong_running_instances: int
    stopped_instances: int
    errors: list[str]


class TaskSummary(BaseModel):
    task_id: str
    assigned_worker_id: str | None
    placement_state: TaskPlacementState
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float | None = None
    targets: list[str]
    enabled: bool
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
        self._reconciled_generation: int | None = None

    async def _verified_collector(self, worker_id: str) -> CollectorPort:
        """返回在线且身份与 placement worker_id 一致的 Collector。"""
        collector = self._collectors.get(worker_id)
        try:
            status = await collector.config_status()
        except Exception as exc:
            raise TaskWorkerUnavailableError(
                f"collector '{worker_id}' is unavailable"
            ) from exc
        reported_id = str(status.get("collector_id") or "")
        if reported_id != worker_id:
            raise TaskWorkerUnavailableError(
                f"collector identity mismatch: expected={worker_id} "
                f"reported={reported_id or '<empty>'}"
            )
        return collector

    @property
    def placement_safe(self) -> bool:
        """当前 placement 代次是否已完成安全收敛。"""
        return self._reconciled_generation == self._assignments.generation

    def _require_safe_start(self) -> None:
        """仅在当前 placement 代次完成安全收敛后允许新的 start。"""
        if not self.placement_safe:
            raise TaskPlacementUnsafeError(
                "task placement is not safely reconciled"
            )

    async def reconcile_placement(self) -> TaskPlacementReconcileResult:
        """停止跑在错误 Collector 上的实例，并建立当前 placement 安全栅栏。"""
        generation = self._assignments.generation
        assignments = {
            row.task_id: row
            for row in self._assignments.list_assignments()
        }
        enabled_tasks = {
            task.task_id
            for task in self._config.current_config.tasks.tasks
            if task.enabled
        }
        orphaned_tasks = sorted(
            row.task_id
            for row in assignments.values()
            if row.state is TaskPlacementState.ORPHANED
        )
        worker_ids = self._collectors.list_worker_ids()

        assigned_by_worker: dict[str, list[str]] = {
            worker_id: sorted(
                task_id
                for task_id, assignment in assignments.items()
                if assignment.state is TaskPlacementState.ASSIGNED
                and assignment.worker_id == worker_id
            )
            for worker_id in worker_ids
        }

        async def fetch(worker_id: str) -> tuple[CollectorPort, list[dict[str, Any]]]:
            collector = await self._verified_collector(worker_id)
            placement = await collector.apply_task_placement(
                worker_id,
                generation,
                assigned_by_worker[worker_id],
            )
            if not bool(placement.get("success")):
                raise RuntimeError("collector rejected task placement")
            if int(placement.get("generation") or 0) != generation:
                raise RuntimeError(
                    "collector placement generation acknowledgment mismatch"
                )
            if int(placement.get("task_count") or 0) != len(
                assigned_by_worker[worker_id]
            ):
                raise RuntimeError(
                    "collector placement task-count acknowledgment mismatch"
                )
            return collector, await collector.list_task_instances()

        results = await asyncio.gather(
            *(fetch(worker_id) for worker_id in worker_ids),
            return_exceptions=True,
        )
        unavailable_workers: list[str] = []
        errors: list[str] = []
        examined_instances = 0
        wrong_running_instances = 0
        stopped_instances = 0

        for worker_id, result in zip(worker_ids, results, strict=True):
            if isinstance(result, BaseException):
                unavailable_workers.append(worker_id)
                errors.append(
                    f"{worker_id}: {str(result) or type(result).__name__}"
                )
                continue

            collector, rows = result
            for row in rows:
                examined_instances += 1
                if str(row.get("state") or "") != TaskInstanceState.RUNNING.value:
                    continue
                task_id = str(row.get("task_id") or "")
                assignment = assignments.get(task_id)
                expected_worker = (
                    assignment.worker_id
                    if task_id in enabled_tasks
                    and assignment is not None
                    and assignment.state is TaskPlacementState.ASSIGNED
                    else None
                )
                if expected_worker == worker_id:
                    continue

                wrong_running_instances += 1
                instance_id = str(row.get("instance_id") or "")
                if not instance_id:
                    errors.append(
                        f"{worker_id}: running task '{task_id}' has empty instance_id"
                    )
                    continue
                try:
                    await collector.stop_task_instance(instance_id)
                except Exception as exc:
                    errors.append(
                        f"{worker_id}/{instance_id}: stop failed: "
                        f"{str(exc) or type(exc).__name__}"
                    )
                else:
                    stopped_instances += 1

        safe = (
            not unavailable_workers
            and not orphaned_tasks
            and not errors
            and self._assignments.generation == generation
        )
        self._reconciled_generation = generation if safe else None
        return TaskPlacementReconcileResult(
            safe=safe,
            generation=generation,
            scanned_workers=len(worker_ids),
            unavailable_workers=unavailable_workers,
            orphaned_tasks=orphaned_tasks,
            examined_instances=examined_instances,
            wrong_running_instances=wrong_running_instances,
            stopped_instances=stopped_instances,
            errors=errors,
        )

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
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id is not None
        ]
        by_worker: dict[str, set[str]] = {}
        for assignment in assignments:
            assert assignment.worker_id is not None
            by_worker.setdefault(assignment.worker_id, set()).add(assignment.task_id)

        worker_ids = sorted(by_worker)
        async def fetch(worker_id: str) -> list[dict[str, Any]]:
            collector = await self._verified_collector(worker_id)
            return await collector.list_task_instances()

        results = await asyncio.gather(
            *(fetch(worker_id) for worker_id in worker_ids),
            return_exceptions=True,
        )
        rows: list[TaskInstanceDetail] = []
        for worker_id, worker_rows in zip(worker_ids, results, strict=True):
            if isinstance(worker_rows, BaseException):
                continue
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
        if assignment.state is not TaskPlacementState.ASSIGNED:
            return self._fallback_summary(cfg, assignment.worker_id)

        assert assignment.worker_id is not None
        try:
            collector = await self._verified_collector(assignment.worker_id)
            rows = await collector.list_tasks()
        except TaskWorkerUnavailableError:
            return self._fallback_summary(cfg, assignment.worker_id)
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
        if assignment.state is not TaskPlacementState.ASSIGNED:
            return []
        assert assignment.worker_id is not None
        try:
            collector = await self._verified_collector(assignment.worker_id)
            rows = await collector.list_task_instances()
        except TaskWorkerUnavailableError:
            return []
        return [
            TaskInstanceDetail.model_validate(
                {**row, "assigned_worker_id": assignment.worker_id}
            )
            for row in rows
            if str(row.get("task_id") or "") == task_id
        ]

    async def start_instance(self, instance_id: str) -> TaskInstanceDetail:
        self._require_safe_start()
        current = await self.get_instance(instance_id)
        collector = await self._verified_collector(current.assigned_worker_id)
        try:
            data = await collector.start_task_instance(
                instance_id,
                self._assignments.generation,
            )
        except CollectorPlacementRejectedError as exc:
            self._reconciled_generation = None
            raise TaskPlacementUnsafeError(str(exc)) from exc
        return TaskInstanceDetail.model_validate(
            {**data, "assigned_worker_id": current.assigned_worker_id}
        )

    async def stop_instance(self, instance_id: str) -> TaskInstanceDetail:
        current = await self.get_instance(instance_id)
        collector = await self._verified_collector(current.assigned_worker_id)
        data = await collector.stop_task_instance(instance_id)
        return TaskInstanceDetail.model_validate(
            {**data, "assigned_worker_id": current.assigned_worker_id}
        )

    async def start_task(self, task_id: str) -> TaskSummary:
        self._require_safe_start()
        worker_id = self._assignments.worker_for_task(task_id)
        collector = await self._verified_collector(worker_id)
        try:
            data = await collector.start_task(
                task_id,
                self._assignments.generation,
            )
        except CollectorPlacementRejectedError as exc:
            self._reconciled_generation = None
            raise TaskPlacementUnsafeError(str(exc)) from exc
        return TaskSummary.model_validate(
            {
                **data,
                "assigned_worker_id": worker_id,
                "placement_state": TaskPlacementState.ASSIGNED,
            }
        )

    async def stop_task(self, task_id: str) -> TaskSummary:
        worker_id = self._assignments.worker_for_task(task_id)
        collector = await self._verified_collector(worker_id)
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
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id is not None
        ]
        by_worker: dict[str, set[str]] = {}
        for assignment in assignments:
            assert assignment.worker_id is not None
            by_worker.setdefault(assignment.worker_id, set()).add(assignment.task_id)

        worker_ids = sorted(by_worker)
        async def fetch(worker_id: str) -> list[dict[str, Any]]:
            collector = await self._verified_collector(worker_id)
            return await collector.list_tasks()

        results = await asyncio.gather(
            *(fetch(worker_id) for worker_id in worker_ids),
            return_exceptions=True,
        )
        rows: list[tuple[str, dict[str, Any]]] = []
        for worker_id, worker_rows in zip(worker_ids, results, strict=True):
            if isinstance(worker_rows, BaseException):
                continue
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
        """构造未分配、孤儿或 Worker 未返回 Task 时的诚实控制面状态。"""
        placement_state = (
            self._assignments.assignment_for_task(cfg.task_id).state
        )
        return TaskSummary(
            task_id=cfg.task_id,
            assigned_worker_id=worker_id,
            placement_state=placement_state,
            device=cfg.device,
            device_group=cfg.device_group,
            point_group=cfg.point_group,
            interval=cfg.interval,
            targets=[target.sink for target in cfg.targets],
            enabled=cfg.enabled,
            runtime_state=(
                "orphaned"
                if placement_state is TaskPlacementState.ORPHANED
                else "unavailable"
                if worker_id is not None
                else "unassigned"
            ),
            instance_count=0,
            running_instances=0,
            stopped_instances=0,
            failed_instances=0,
        )

    async def start_all_instances(self) -> TaskBatchResult:
        """按 assignment 启动全部 Task Instance。"""
        self._require_safe_start()
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
            collector = await self._verified_collector(row.assigned_worker_id)
            if start:
                try:
                    await collector.start_task_instance(
                        row.instance_id,
                        self._assignments.generation,
                    )
                except CollectorPlacementRejectedError as exc:
                    self._reconciled_generation = None
                    raise TaskPlacementUnsafeError(str(exc)) from exc
            else:
                await collector.stop_task_instance(row.instance_id)

        await asyncio.gather(*(apply(row) for row in changed))
        return TaskBatchResult(
            total=len(instances),
            changed=len(changed),
            unchanged=len(instances) - len(changed),
        )
