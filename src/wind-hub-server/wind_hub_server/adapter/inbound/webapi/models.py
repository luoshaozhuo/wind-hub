"""API request/response models.

Models are defined separately from the domain layer so the wire contract can
evolve without leaking domain types.  Fields map one-to-one onto the domain
models they mirror.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Engine health snapshot, derived from ``QueryUseCase.status()``."""

    status: str
    """``"ok"`` when the engine is running, otherwise ``"down"``."""

    running: bool
    device_count: int
    sink_count: int
    devices_connected: int
    sinks_healthy: int
    points_collected: int
    """累计采集点数（进程级单调计数）。"""
    points_routed: int
    """累计路由点数。"""
    points_dropped: int
    """背压累计丢弃点数。"""


class DeviceInfoResponse(BaseModel):
    device_id: str
    protocol: str
    connected: bool
    last_seen: datetime | None = None


class PointValueResponse(BaseModel):
    device_id: str
    point_id: str
    value: Any
    quality: str
    timestamp: datetime
    source: str | None = None


class CommandRequest(BaseModel):
    device_id: str
    point_id: str
    value: Any
    timeout: float = 5.0
    command_id: str | None = None
    """Optional idempotency key; generated server-side when omitted."""


class CommandResponse(BaseModel):
    command_id: str
    success: bool
    error: str | None = None
    finished_at: datetime


class ReloadResponse(BaseModel):
    success: bool
    devices_added: list[str]
    devices_removed: list[str]
    devices_updated: list[str]
    sinks_added: list[str]
    sinks_removed: list[str]
    sinks_updated: list[str]
    tasks_added: list[str]
    tasks_removed: list[str]
    tasks_updated: list[str]
    errors: list[str]
    duration_ms: float
    reloaded_at: datetime


class TaskResponse(BaseModel):
    """采集 Task 定义快照（来自 tasks.yaml）。"""

    task_id: str
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float | None
    targets: list[str]
    enabled: bool


class TaskInstanceResponse(BaseModel):
    """Task Instance（task × device 展开）的生命周期快照。"""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    interval: float | None
    targets: list[str]
    state: str
    """``"running"`` / ``"stopped"``。"""


class TaskBatchResponse(BaseModel):
    """批量实例操作（start-all / stop-all）的结果汇总。"""

    total: int
    changed: int
    unchanged: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorDetail


__all__ = [
    "HealthResponse",
    "DeviceInfoResponse",
    "PointValueResponse",
    "CommandRequest",
    "CommandResponse",
    "ReloadResponse",
    "TaskResponse",
    "TaskInstanceResponse",
    "TaskBatchResponse",
    "ErrorDetail",
    "ErrorResponse",
]
