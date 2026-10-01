"""Admin API v1 的 wire DTO。

DTO 与 application/domain 模型分离，保证 HTTP 契约可以独立演进。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PageMeta(BaseModel):
    """统一分页元数据。"""

    page: int
    page_size: int
    total: int


class DeviceResponse(BaseModel):
    """设备列表/详情 DTO。"""

    device_id: str
    protocol: str
    host: str
    port: int
    point_table: str
    device_type: str | None = None
    model: str | None = None
    device_group: str | None = None
    enabled: bool
    connected: bool
    consecutive_failures: int
    last_error: str | None = None


class DevicePageResponse(BaseModel):
    """设备分页响应。"""

    items: list[DeviceResponse]
    page: PageMeta


class TaskResponse(BaseModel):
    """Task Definition 与实例聚合运行状态 DTO。"""

    task_id: str
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float | None
    targets: list[str]
    enabled: bool
    runtime_state: str
    instance_count: int
    running_instances: int
    stopped_instances: int
    failed_instances: int


class TaskPageResponse(BaseModel):
    """Task 分页响应。"""

    items: list[TaskResponse]
    page: PageMeta


class TaskInstanceResponse(BaseModel):
    """指定 Task 展开的实例 DTO。"""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    interval: float | None
    targets: list[str]
    state: str


class OverviewResponse(BaseModel):
    """Overview 核心运行快照 DTO。"""

    site_id: str | None = None
    site_name: str | None = None
    runtime_running: bool
    device_count: int
    devices_connected: int
    devices_offline: int
    sink_count: int
    sinks_healthy: int
    task_count: int
    task_instances: int
    task_instances_running: int
    task_instances_failed: int
    points_collected: int
    points_routed: int
    points_dropped: int


class OperationErrorResponse(BaseModel):
    """Operation 错误 DTO。"""

    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class OperationResponse(BaseModel):
    """异步 Operation 查询 DTO。"""

    operation_id: str
    kind: str
    state: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int
    completed: int
    progress: float
    result: dict[str, object] | None = None
    error: OperationErrorResponse | None = None
