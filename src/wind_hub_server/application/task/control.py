"""基于 Collector gRPC 的 Task 控制服务。

TaskControlService 负责 Task/Task Instance 的查询与显式 start/stop；placement
状态归 TaskPlacementRegistry，实际收敛与安全栅栏归 TaskPlacementReconciler。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.monitoring import MonitoringSnapshotPort
from wind_hub_server.application.port.worker import (
    CollectorPlacementRejectedError,
    CollectorTaskInstance,
)
from wind_hub_server.application.task.collector import (
    TaskWorkerUnavailableError,
    verified_collector,
)
from wind_hub_server.application.task.model import TaskInstanceDetail, TaskSummary
from wind_hub_server.application.task.placement import (
    TaskPlacementRegistry,
    TaskPlacementState,
)
from wind_hub_server.application.task.reconcile import (
    TaskPlacementReconciler,
    TaskPlacementUnsafeError,
)


class TaskControlService:
    """Server 的 Task facade；所有生命周期操作发往独立 Collector。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        placements: TaskPlacementRegistry,
        reconciler: TaskPlacementReconciler,
        config: ConfigService,
        monitoring: MonitoringSnapshotPort,
    ) -> None:
        self._collectors = collectors
        self._placements = placements
        self._reconciler = reconciler
        self._config = config
        self._monitoring = monitoring

    def list_task_summaries(self) -> list[TaskSummary]:
        """从 Monitoring 最近一次快照返回 Task placement 与运行状态。"""
        runtime_rows = {
            row.task_id: row for row in self._monitoring.tasks_snapshot()
        }
        rows: list[TaskSummary] = []
        for cfg in self._config.current_config.tasks.tasks:
            placement = self._placements.placement_for_task(cfg.task_id)
            current = runtime_rows.get(cfg.task_id)
            if current is not None:
                rows.append(
                    TaskSummary.model_validate(
                        {
                            **asdict(current),
                            # 配置定义以 Server 当前成功基线为权威；Monitoring
                            # 仅提供运行态/实例计数，避免配置 Apply 后仍暴露旧值。
                            "device": cfg.device,
                            "device_group": cfg.device_group,
                            "point_group": cfg.point_group,
                            "interval": cfg.interval,
                            "targets": [target.sink for target in cfg.targets],
                            "enabled": cfg.enabled,
                            "assigned_worker_id": current.assigned_worker_id
                            or placement.worker_id,
                            "placement_state": placement.state,
                        }
                    )
                )
                continue
            rows.append(self._fallback_summary(cfg, placement.worker_id))
        return rows

    async def list_instances(self) -> list[TaskInstanceDetail]:
        placements = [
            row
            for row in self._placements.list_placements()
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id is not None
        ]
        by_worker: dict[str, set[str]] = {}
        for placement in placements:
            assert placement.worker_id is not None
            by_worker.setdefault(placement.worker_id, set()).add(placement.task_id)

        worker_ids = sorted(by_worker)
        async def fetch(worker_id: str) -> list[CollectorTaskInstance]:
            collector = await verified_collector(self._collectors, worker_id)
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
                    {**asdict(row), "assigned_worker_id": worker_id}
                )
                for row in worker_rows
                if row.task_id in assigned_task_ids
            )
        return rows

    async def get_instance(self, instance_id: str) -> TaskInstanceDetail:
        rows = await self.list_instances()
        for row in rows:
            if row.instance_id == instance_id:
                return row
        raise KeyError(instance_id)

    def get_task_summary(self, task_id: str) -> TaskSummary:
        """从最近一次 Monitoring 快照返回指定 Task 状态。"""
        for row in self.list_task_summaries():
            if row.task_id == task_id:
                return row
        raise KeyError(task_id)

    async def list_task_instances(self, task_id: str) -> list[TaskInstanceDetail]:
        """返回指定 Task 当前展开的全部实例；未分配 Task 返回空列表。"""
        placement = self._placements.placement_for_task(task_id)
        if placement.state is not TaskPlacementState.ASSIGNED:
            return []
        assert placement.worker_id is not None
        try:
            collector = await verified_collector(self._collectors, placement.worker_id)
            rows = await collector.list_task_instances()
        except TaskWorkerUnavailableError:
            return []
        return [
            TaskInstanceDetail.model_validate(
                {**asdict(row), "assigned_worker_id": placement.worker_id}
            )
            for row in rows
            if row.task_id == task_id
        ]

    async def start_instance(self, instance_id: str) -> TaskInstanceDetail:
        self._reconciler.require_safe_start()
        current = await self.get_instance(instance_id)
        collector = await verified_collector(self._collectors, current.assigned_worker_id)
        try:
            data = await collector.start_task_instance(
                instance_id,
                self._placements.generation,
            )
        except CollectorPlacementRejectedError as exc:
            self._reconciler.invalidate_safety()
            raise TaskPlacementUnsafeError(str(exc)) from exc
        result = TaskInstanceDetail.model_validate(
            {**asdict(data), "assigned_worker_id": current.assigned_worker_id}
        )
        await self._monitoring.refresh_now()
        return result

    async def stop_instance(self, instance_id: str) -> TaskInstanceDetail:
        current = await self.get_instance(instance_id)
        collector = await verified_collector(self._collectors, current.assigned_worker_id)
        data = await collector.stop_task_instance(instance_id)
        result = TaskInstanceDetail.model_validate(
            {**asdict(data), "assigned_worker_id": current.assigned_worker_id}
        )
        await self._monitoring.refresh_now()
        return result

    async def start_task(self, task_id: str) -> TaskSummary:
        self._reconciler.require_safe_start()
        worker_id = self._placements.worker_for_task(task_id)
        collector = await verified_collector(self._collectors, worker_id)
        try:
            data = await collector.start_task(
                task_id,
                self._placements.generation,
            )
        except CollectorPlacementRejectedError as exc:
            self._reconciler.invalidate_safety()
            raise TaskPlacementUnsafeError(str(exc)) from exc
        result = TaskSummary.model_validate(
            {
                **asdict(data),
                "assigned_worker_id": worker_id,
                "placement_state": TaskPlacementState.ASSIGNED,
            }
        )
        await self._monitoring.refresh_now()
        return result

    async def stop_task(self, task_id: str) -> TaskSummary:
        worker_id = self._placements.worker_for_task(task_id)
        collector = await verified_collector(self._collectors, worker_id)
        data = await collector.stop_task(task_id)
        result = TaskSummary.model_validate(
            {
                **asdict(data),
                "assigned_worker_id": worker_id,
                "placement_state": TaskPlacementState.ASSIGNED,
            }
        )
        await self._monitoring.refresh_now()
        return result

    def _fallback_summary(
        self,
        cfg: Any,
        worker_id: str | None,
    ) -> TaskSummary:
        """构造未分配、孤儿或 Worker 未返回 Task 时的诚实控制面状态。"""
        placement_state = (
            self._placements.placement_for_task(cfg.task_id).state
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


__all__ = ["TaskControlService"]
