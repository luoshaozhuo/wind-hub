"""Task placement 实际收敛。

TaskPlacementReconciler 是 placement safety 的唯一权威：它把期望 placement
下发到各 Collector、停止跑在错误 Collector 上的实例，并只在当前 generation
完整收敛成功后才打开 start 安全栅栏。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.worker import (
    CollectorPort,
    CollectorTaskInstance,
)
from wind_hub_server.application.task.collector import verified_collector
from wind_hub_server.application.task.model import TaskInstanceState
from wind_hub_server.application.task.placement import (
    TaskPlacementRegistry,
    TaskPlacementState,
)


class TaskPlacementUnsafeError(RuntimeError):
    """当前 placement 尚未完成安全收敛，禁止新的 start 操作。"""


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


class TaskPlacementReconciler:
    """收敛 Collector 实际 Task Instance 与期望 placement。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        placements: TaskPlacementRegistry,
        config: ConfigService,
    ) -> None:
        self._collectors = collectors
        self._placements = placements
        self._config = config
        self._reconciled_generation: int | None = None

    @property
    def placement_safe(self) -> bool:
        """当前 placement 代次是否已完成安全收敛。"""
        return self._reconciled_generation == self._placements.generation

    def require_safe_start(self) -> None:
        """仅在当前 placement 代次完成安全收敛后允许新的 start。"""
        if not self.placement_safe:
            raise TaskPlacementUnsafeError(
                "task placement is not safely reconciled"
            )

    def invalidate_safety(self) -> None:
        """Collector 拒绝受 placement 保护的操作后重新关闭安全栅栏。"""
        self._reconciled_generation = None

    async def reconcile(self) -> TaskPlacementReconcileResult:
        """停止跑在错误 Collector 上的实例，并建立当前 placement 安全栅栏。"""
        generation = self._placements.generation
        placements = {
            row.task_id: row
            for row in self._placements.list_placements()
        }
        enabled_tasks = {
            task.task_id
            for task in self._config.current_config.tasks.tasks
            if task.enabled
        }
        orphaned_tasks = sorted(
            row.task_id
            for row in placements.values()
            if row.state is TaskPlacementState.ORPHANED
        )
        worker_ids = self._collectors.list_worker_ids()

        assigned_by_worker: dict[str, list[str]] = {
            worker_id: sorted(
                task_id
                for task_id, placement in placements.items()
                if placement.state is TaskPlacementState.ASSIGNED
                and placement.worker_id == worker_id
            )
            for worker_id in worker_ids
        }

        async def fetch(
            worker_id: str,
        ) -> tuple[CollectorPort, list[CollectorTaskInstance]]:
            collector = await verified_collector(self._collectors, worker_id)
            placement = await collector.apply_task_placement(
                worker_id,
                generation,
                assigned_by_worker[worker_id],
            )
            if not placement.success:
                raise RuntimeError("collector rejected task placement")
            if placement.generation != generation:
                raise RuntimeError(
                    "collector placement generation acknowledgment mismatch"
                )
            if placement.task_count != len(assigned_by_worker[worker_id]):
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
                if row.state != TaskInstanceState.RUNNING.value:
                    continue
                task_id = row.task_id
                placement = placements.get(task_id)
                expected_worker = (
                    placement.worker_id
                    if task_id in enabled_tasks
                    and placement is not None
                    and placement.state is TaskPlacementState.ASSIGNED
                    else None
                )
                if expected_worker == worker_id:
                    continue

                wrong_running_instances += 1
                instance_id = row.instance_id
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
            and self._placements.generation == generation
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


__all__ = [
    "TaskPlacementReconcileResult",
    "TaskPlacementReconciler",
    "TaskPlacementUnsafeError",
]
