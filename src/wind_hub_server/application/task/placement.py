"""Server Task placement 控制面状态。"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel

from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.port.collector_directory import CollectorDirectory

_STATE_VERSION = 2


class TaskPlacementState(StrEnum):
    """Task 当前 placement 状态。"""

    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    ORPHANED = "orphaned"


class TaskPlacement(BaseModel):
    """单个 Task 的 Collector placement。"""

    task_id: str
    worker_id: str | None
    state: TaskPlacementState


class TaskPlacementError(RuntimeError):
    """Task 当前没有可执行 placement。"""


class TaskPlacementRegistry:
    """Server 持有并持久化的 Task placement 状态。

    首次启动使用 rendezvous hashing 建立稳定 placement；之后从状态文件恢复。
    已有 Owner 不再登记时保留原 worker_id 并标记 ORPHANED，禁止自动漂移到新
    Collector，以避免 Server 重启/Worker 集合变化后形成重复采集。
    """

    def __init__(
        self,
        config: ConfigService,
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
        """返回当前 placement 代次（纯读取）；placement 变化时单调递增。

        本 getter 不做任何同步或持久化；需要与当前配置/Worker 集合对齐时，
        调用方必须先显式调用 :meth:`sync`。
        """
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

    def placement_for_task(self, task_id: str) -> TaskPlacement:
        """返回指定 Task placement；未知 Task 抛 KeyError。"""
        self.sync()
        if task_id not in self._placements:
            raise KeyError(task_id)
        worker_id = self._placements[task_id]
        return TaskPlacement(
            task_id=task_id,
            worker_id=worker_id,
            state=self._state_for(worker_id),
        )

    def worker_for_task(self, task_id: str) -> str:
        """返回可执行 Collector；未分配/孤儿 placement 抛 TaskPlacementError。"""
        placement = self.placement_for_task(task_id)
        if placement.state is TaskPlacementState.UNASSIGNED:
            raise TaskPlacementError(f"task '{task_id}' is unassigned")
        if placement.state is TaskPlacementState.ORPHANED:
            raise TaskPlacementError(
                f"task '{task_id}' is orphaned from worker "
                f"'{placement.worker_id}'"
            )
        assert placement.worker_id is not None
        return placement.worker_id

    def list_placements(self) -> list[TaskPlacement]:
        """返回当前成功配置中全部 Task placement。"""
        self.sync()
        return [
            TaskPlacement(
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
            for row in self.list_placements()
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id == worker_id
        ]

    def worker_ids_for_device(self, device_id: str) -> list[str]:
        """返回实际承载指定设备采集 Task 的当前 Collector。"""
        devices = self._config.current_config.devices.devices
        device = next((item for item in devices if item.device_id == device_id), None)
        if device is None:
            raise KeyError(device_id)

        placements = {
            row.task_id: row.worker_id
            for row in self.list_placements()
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id is not None
        }
        workers: set[str] = set()
        for task in self._config.current_config.tasks.tasks:
            if not task.enabled or task.task_id not in placements:
                continue
            matches = (
                task.device == device_id
                if task.device is not None
                else task.device_group == device.device_group
            )
            if matches:
                workers.add(placements[task.task_id])
        return sorted(workers)

    def worker_ids_for_sink(self, sink_name: str) -> list[str]:
        """返回实际向指定 Sink 写入的当前 Collector。"""
        if not any(
            sink.name == sink_name
            for sink in self._config.current_config.sinks.sinks
        ):
            raise KeyError(sink_name)

        placements = {
            row.task_id: row.worker_id
            for row in self.list_placements()
            if row.state is TaskPlacementState.ASSIGNED
            and row.worker_id is not None
        }
        workers = {
            placements[task.task_id]
            for task in self._config.current_config.tasks.tasks
            if (
                task.enabled
                and task.task_id in placements
                and any(target.sink == sink_name for target in task.targets)
            )
        }
        return sorted(workers)


__all__ = [
    "TaskPlacement",
    "TaskPlacementRegistry",
    "TaskPlacementError",
    "TaskPlacementState",
]
