"""Task 控制面读模型。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from wind_hub_server.application.task.placement import TaskPlacementState


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


__all__ = ["TaskInstanceDetail", "TaskInstanceState", "TaskSummary"]
