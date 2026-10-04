"""Server Task placement 控制面状态。"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.usecase.config import ConfigUseCase

_STATE_VERSION = 2


class TaskPlacementState(StrEnum):
    """Task 当前 placement 状态。"""

    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    ORPHANED = "orphaned"


class TaskAssignment(BaseModel):
    """单个 Task 的 Collector placement。"""

    task_id: str
    worker_id: str | None
    state: TaskPlacementState


class TaskPlacementError(RuntimeError):
    """Task 当前没有可执行 placement。"""


class TaskAssignmentUseCase:
    """Server 持有并持久化的 Task placement 状态。

    首次启动使用 rendezvous hashing 建立稳定 placement；之后从状态文件恢复。
    已有 Owner 不再登记时保留原 worker_id 并标记 ORPHANED，禁止自动漂移到新
    Collector，以避免 Server 重启/Worker 集合变化后形成重复采集。
    """

    def __init__(
        self,
        config: ConfigUseCase,
        collectors: CollectorDirectory,
    ) -> None:
        self._config = config
        self._collectors = collectors
        self._state_path = config.config_dir / ".state" / "task-placement.json"
        self._placements: dict[str, str | None] = {}
        self._generation = 0
        if self._state_path.exists():
            self._load()
            self._sync_worker_set()
            self.sync()
        else:
            self._initialize()
            self._persist()

    @property
    def generation(self) -> int:
        """返回当前 placement 代次；placement 变化时单调递增。"""
        self.sync()
        return self._generation


    def _initialize(self) -> None:
        """为首次启动时的 Task 建立稳定 placement。"""
        worker_ids = self._collectors.list_worker_ids()
        self._placements = {
            task.task_id: self._select_worker(task.task_id, worker_ids)
            for task in self._config.current_config.tasks.tasks
        }
        self._persisted_workers = tuple(sorted(worker_ids))
        self._generation = 1

    def _load(self) -> None:
        """读取并校验已持久化 placement。"""
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
            raise ValueError(
                f"unsupported task placement state: {self._state_path}"
            )
        placements = raw.get("placements")
        if not isinstance(placements, dict):
            raise ValueError(
                f"invalid task placement state: {self._state_path}"
            )

        loaded: dict[str, str | None] = {}
        for task_id, worker_id in placements.items():
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(
                    f"invalid task_id in placement state: {self._state_path}"
                )
            if worker_id is not None and (
                not isinstance(worker_id, str) or not worker_id
            ):
                raise ValueError(
                    f"invalid worker_id for task '{task_id}' in placement state"
                )
            loaded[task_id] = worker_id
        generation = raw.get("generation")
        if not isinstance(generation, int) or generation < 1:
            raise ValueError(
                f"invalid placement generation: {self._state_path}"
            )
        workers = raw.get("workers")
        if (
            not isinstance(workers, list)
            or any(not isinstance(worker_id, str) or not worker_id for worker_id in workers)
            or len(set(workers)) != len(workers)
        ):
            raise ValueError(
                f"invalid placement worker set: {self._state_path}"
            )
        self._placements = loaded
        self._generation = generation
        self._persisted_workers = tuple(sorted(workers))

    def _persist(self) -> None:
        """原子写入 placement 状态文件。"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STATE_VERSION,
            "generation": self._generation,
            "workers": list(self._persisted_workers),
            "placements": dict(sorted(self._placements.items())),
        }
        temp_path = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.tmp"
        )
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._state_path)

    def _changed(self) -> None:
        """记录 placement 变化并持久化。"""
        self._generation += 1
        self._persist()

    def _sync_worker_set(self) -> None:
        """Worker 拓扑变化时递增 placement generation，但不迁移已有 Owner。"""
        current = tuple(sorted(self._collectors.list_worker_ids()))
        if current == self._persisted_workers:
            return
        self._persisted_workers = current
        self._changed()

    def sync(self) -> None:
        """与当前成功配置和 Worker 集合同步，不迁移已有 Task。"""
        self._sync_worker_set()
        task_ids = {
            task.task_id
            for task in self._config.current_config.tasks.tasks
        }
        changed = False

        for task_id in list(self._placements):
            if task_id not in task_ids:
                del self._placements[task_id]
                changed = True

        worker_ids = self._collectors.list_worker_ids()
        for task_id in sorted(task_ids - set(self._placements)):
            self._placements[task_id] = self._select_worker(task_id, worker_ids)
            changed = True

        if changed:
            self._changed()

    @staticmethod
    def _select_worker(task_id: str, worker_ids: list[str]) -> str | None:
        """用 rendezvous hashing 选择稳定 Worker；无 Worker 时返回 None。"""
        if not worker_ids:
            return None
        return max(
            worker_ids,
            key=lambda worker_id: sha256(
                f"{task_id}\0{worker_id}".encode()
            ).digest(),
        )

    def _state_for(self, worker_id: str | None) -> TaskPlacementState:
        """根据当前 Worker 目录解释持久化 Owner。"""
        if worker_id is None:
            return TaskPlacementState.UNASSIGNED
        if worker_id in set(self._collectors.list_worker_ids()):
            return TaskPlacementState.ASSIGNED
        return TaskPlacementState.ORPHANED

    def assignment_for_task(self, task_id: str) -> TaskAssignment:
        """返回指定 Task placement；未知 Task 抛 KeyError。"""
        self.sync()
        if task_id not in self._placements:
            raise KeyError(task_id)
        worker_id = self._placements[task_id]
        return TaskAssignment(
            task_id=task_id,
            worker_id=worker_id,
            state=self._state_for(worker_id),
        )

    def worker_for_task(self, task_id: str) -> str:
        """返回可执行 Collector；未分配/孤儿 placement 抛 TaskPlacementError。"""
        assignment = self.assignment_for_task(task_id)
        if assignment.state is TaskPlacementState.UNASSIGNED:
            raise TaskPlacementError(f"task '{task_id}' is unassigned")
        if assignment.state is TaskPlacementState.ORPHANED:
            raise TaskPlacementError(
                f"task '{task_id}' is orphaned from worker "
                f"'{assignment.worker_id}'"
            )
        assert assignment.worker_id is not None
        return assignment.worker_id

    def list_assignments(self) -> list[TaskAssignment]:
        """返回当前成功配置中全部 Task placement。"""
        self.sync()
        return [
            TaskAssignment(
                task_id=task_id,
                worker_id=worker_id,
                state=self._state_for(worker_id),
            )
            for task_id, worker_id in sorted(self._placements.items())
        ]

    def task_ids_for_worker(self, worker_id: str) -> list[str]:
        """返回当前确实分配给指定 Worker 的 Task ID。"""
        self._collectors.get(worker_id)
        return [
            row.task_id
            for row in self.list_assignments()
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id == worker_id
        ]

    def worker_ids_for_device(self, device_id: str) -> list[str]:
        """返回实际承载指定设备采集 Task 的当前 Collector。"""
        devices = self._config.current_config.devices.devices
        device = next((item for item in devices if item.device_id == device_id), None)
        if device is None:
            raise KeyError(device_id)

        assignments = {
            row.task_id: row.worker_id
            for row in self.list_assignments()
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id is not None
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
        """返回实际向指定 Sink 写入的当前 Collector。"""
        if not any(
            sink.name == sink_name
            for sink in self._config.current_config.sinks.sinks
        ):
            raise KeyError(sink_name)

        assignments = {
            row.task_id: row.worker_id
            for row in self.list_assignments()
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id is not None
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
