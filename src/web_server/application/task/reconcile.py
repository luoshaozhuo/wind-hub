"""Task placement 实际收敛。

Server 自己计算任务目标 Worker，停止错误 Worker 的采集；
Collector 只维护设备级期望状态，不接受 placement generation。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from web_server.application.config.service import ConfigService
from web_server.application.port.collector_directory import CollectorDirectory
from web_server.application.port.worker import (
    CollectorPort,
    CollectorTaskInstance,
)
from web_server.application.task.collector import verified_collector
from web_server.application.task.model import TaskInstanceState
from web_server.application.task.placement import (
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
        """当前 placement 代次是否已完成安全收敛（纯读取，不做同步）。"""
        return self._reconciled_generation == self._placements.generation

    def require_safe_start(self) -> None:
        """仅在当前 placement 代次完成安全收敛后允许新的 start。"""
        self._placements.sync()
        if not self.placement_safe:
            raise TaskPlacementUnsafeError("task placement is not safely reconciled")

    def invalidate_safety(self) -> None:
        """Collector 拒绝受 placement 保护的操作后重新关闭安全栅栏。"""
        self._reconciled_generation = None

    async def reconcile(self) -> TaskPlacementReconcileResult:
        """停止跑在错误 Collector 上的实例，并建立当前 placement 安全栅栏。"""
        self._placements.sync()
        generation = self._placements.generation
        placements = {row.task_id: row for row in self._placements.list_placements()}
        enabled_tasks = {
            task.task_id for task in self._config.current_config.tasks.values() if task.enabled
        }
        orphaned_tasks = sorted(
            row.task_id for row in placements.values() if row.state is TaskPlacementState.ORPHANED
        )
        device_tasks: dict[str, set[str]] = {}
        for task in self._config.current_config.tasks.values():
            for device in self._config.current_config.site.devices.values():
                if task.device_group_id in device.device_group_ids and device.enabled:
                    device_tasks.setdefault(str(device.device_id), set()).add(task.task_id)
        # 同一设备被多个启用 Task 分配到不同 Worker 是归属冲突：显式报告并
        # 保持 unsafe，禁止把冲突设备上的实例按「错误 Worker」静默停止。
        expected_workers_by_device: dict[str, set[str]] = {}
        for device_id, task_ids in device_tasks.items():
            workers: set[str] = set()
            for task_id in task_ids & enabled_tasks:
                placement = placements.get(task_id)
                if (
                    placement is not None
                    and placement.state is TaskPlacementState.ASSIGNED
                    and placement.worker_id is not None
                ):
                    workers.add(placement.worker_id)
            expected_workers_by_device[device_id] = workers
        conflicting_devices = {
            device_id
            for device_id, workers in expected_workers_by_device.items()
            if len(workers) > 1
        }
        worker_ids = self._collectors.list_worker_ids()

        async def fetch(
            worker_id: str,
        ) -> tuple[CollectorPort, list[CollectorTaskInstance]]:
            collector = await verified_collector(self._collectors, worker_id)
            return collector, await collector.list_task_instances()

        results = await asyncio.gather(
            *(fetch(worker_id) for worker_id in worker_ids),
            return_exceptions=True,
        )
        unavailable_workers: list[str] = []
        errors: list[str] = [
            f"device '{device_id}' ownership conflict: enabled tasks assign it to "
            f"multiple workers {sorted(expected_workers_by_device[device_id])}"
            for device_id in sorted(conflicting_devices)
        ]
        examined_instances = 0
        wrong_running_instances = 0
        stopped_instances = 0

        for worker_id, result in zip(worker_ids, results, strict=True):
            if isinstance(result, BaseException):
                unavailable_workers.append(worker_id)
                errors.append(f"{worker_id}: {str(result) or type(result).__name__}")
                continue

            collector, rows = result
            for row in rows:
                examined_instances += 1
                if row.state != TaskInstanceState.RUNNING.value:
                    continue
                expected_workers = expected_workers_by_device.get(row.device_id, set())
                if row.device_id in conflicting_devices:
                    # 归属冲突已在 errors 报告：两个 Worker 都可能是合法属主，
                    # 停止任何一侧都会以静默 STOP 掩盖冲突。
                    continue
                if expected_workers == {worker_id}:
                    continue
                task_id = row.task_id

                wrong_running_instances += 1
                instance_id = row.instance_id
                if not instance_id:
                    errors.append(f"{worker_id}: running task '{task_id}' has empty instance_id")
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
