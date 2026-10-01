"""Admin API v1 的 wire DTO。

DTO 与 application/domain 模型分离，保证 HTTP 契约可以独立演进。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


class DeviceDataItemResponse(BaseModel):
    """Devices Data 单点最新值 DTO。"""

    point_id: str
    variable_name: str | None = None
    point_groups: list[str]
    device_groups: list[str]
    data_type: str
    unit: str
    unit_symbol: str
    description: str | None = None
    value: Any = None
    quality: str | None = None
    timestamp: datetime | None = None
    source: str | None = None


class DeviceDataPageResponse(BaseModel):
    """Devices Data 分页响应。"""

    items: list[DeviceDataItemResponse]
    page: PageMeta


class TrendSampleResponse(BaseModel):
    """趋势单个采样点 DTO。"""

    value: object | None = None
    quality: str
    timestamp: datetime
    source: str | None = None


class TrendSeriesResponse(BaseModel):
    """单变量趋势序列 DTO。"""

    point_id: str
    variable_name: str | None = None
    unit: str
    unit_symbol: str
    samples: list[TrendSampleResponse]


class DeviceCommandRequest(BaseModel):
    """设备控制请求。"""

    point_id: str
    value: Any
    timeout: float = Field(default=5.0, gt=0, le=30)
    command_id: str | None = None


class DeviceCommandResponse(BaseModel):
    """写入确认和真实回读结果。"""

    command_id: str
    requested: Any
    success: bool
    error: str | None = None
    sent_at: datetime
    finished_at: datetime
    latency_ms: float
    readback: Any = None
    readback_timestamp: datetime | None = None
    readback_quality: str | None = None
    readback_error: str | None = None


class ConfigFileResponse(BaseModel):
    name: str
    exists: bool
    optional: bool


class ConfigContentResponse(BaseModel):
    name: str
    content: str


class ConfigTextRequest(BaseModel):
    name: str
    content: str
    comment: str = ""


class ConfigReviewResponse(BaseModel):
    name: str
    valid: bool
    changed: bool
    errors: list[str]
    diff: dict[str, object]


class ConfigApplyResponse(BaseModel):
    success: bool
    revision: int | None = None
    errors: list[str]
    rollback_performed: bool


class ConfigRevisionResponse(BaseModel):
    revision: int
    created_at: datetime
    source: str
    comment: str


class SettingsResponse(BaseModel):
    site_id: str
    site_name: str | None = None
    api_enabled: bool
    api_host: str
    api_port: int
    ads_local_ip: str | None = None
    ads_local_ams_net_id: str | None = None
    ads_username: str = "Administrator"
    ads_password: str = ""


class SettingsRequest(SettingsResponse):
    api_port: int = Field(ge=1, le=65535)


class DefinitionsResponse(BaseModel):
    units: dict[str, dict[str, Any]]
    device_types: dict[str, dict[str, Any]]
    device_models: dict[str, dict[str, Any]]
    point_tables: dict[str, dict[str, Any]]
    point_groups: list[str]


class DefinitionUpsertRequest(BaseModel):
    value: dict[str, Any]


class SinkResponse(BaseModel):
    name: str
    type: str
    enabled: bool
    params: dict[str, Any]
    healthy: bool
    message: str | None = None
    queue_depth: int


class SinkUpsertRequest(BaseModel):
    type: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class SinkTestResponse(BaseModel):
    success: bool
    latency_ms: float
    message: str | None = None
    steps: list[dict[str, object]] = Field(default_factory=list)


class PingRequest(BaseModel):
    host: str
    timeout: float = Field(default=1.0, gt=0, le=10)


class PingResponse(BaseModel):
    host: str
    reachable: bool
    latency_ms: float


class PortsRequest(BaseModel):
    host: str
    ports: list[int]
    timeout: float = Field(default=1.0, gt=0, le=10)


class PortProbeResponse(BaseModel):
    port: int
    state: str
    latency_ms: float


class SubnetScanRequest(BaseModel):
    network: str
    timeout: float = Field(default=0.5, gt=0, le=10)
    ports: list[int] = Field(default_factory=lambda: [502, 2404, 48898])


class ProtocolCheckRequest(BaseModel):
    device_id: str


class ProtocolCheckResponse(BaseModel):
    device_id: str
    connected: bool


class ProtocolReadRequest(BaseModel):
    device_id: str
    point_id: str


class ProtocolReadResponse(BaseModel):
    device_id: str
    point_id: str
    value: Any = None
    quality: str
    timestamp: datetime
    source: str | None = None


class PointTableTestRequest(BaseModel):
    device_id: str


class ProtocolWriteRequest(BaseModel):
    device_id: str
    point_id: str
    value: Any
