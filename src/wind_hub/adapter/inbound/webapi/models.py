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
    """Engine health snapshot, derived from ``TaskUseCase.status()``."""

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
    routing_rebuilt: bool
    pipeline_rebuilt: bool
    errors: list[str]
    duration_ms: float
    reloaded_at: datetime


class RouteExplainResponse(BaseModel):
    device_id: str
    point_id: str
    targets: list[str]
    matched_rule: str | None = None
    source: str


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
    "RouteExplainResponse",
    "ErrorDetail",
    "ErrorResponse",
]
