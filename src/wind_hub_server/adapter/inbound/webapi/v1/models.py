"""Admin API v1 的 wire DTO。

DTO 与 application/domain 模型分离，保证 HTTP 契约可以独立演进。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class PageMeta(BaseModel):
    """统一分页元数据。"""

    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    total: int = Field(ge=0)


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
    extensions: dict[str, Any] = Field(default_factory=dict)
    port_override: int | None = None
    extension_overrides: dict[str, Any] = Field(default_factory=dict)
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
    assigned_worker_id: str | None
    placement_state: str
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
    assigned_worker_id: str
    task_id: str
    device_id: str
    point_group: str
    interval: float | None
    targets: list[str]
    state: str


class WorkerResponse(BaseModel):
    """Worker Registry 状态 DTO。"""

    worker_id: str
    role: str
    endpoint: str
    capabilities: list[str]
    reported_id: str | None = None
    state: str
    last_probe_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_error: str | None = None
    runtime_running: bool | None = None
    active_revision: str | None = None
    active_config_hash: str | None = None
    boot_id: str | None = None


class OverviewResponse(BaseModel):
    """Overview 核心运行快照 DTO。"""

    site_id: str | None = None
    site_name: str | None = None
    runtime_running: bool
    runtime_state: str
    workers_unavailable: list[str] = Field(default_factory=list)
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
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    progress: float = Field(ge=0.0, le=1.0)
    result: dict[str, object] | None = None
    error: OperationErrorResponse | None = None


class DeviceDataItemResponse(BaseModel):
    """Devices Data 单点最新值 DTO。"""

    point_id: str
    variable_name: str | None = None
    point_groups: list[str]
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

    value: Any = None
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
    device_groups: list[str]


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
    ports: list[int] = Field(min_length=1, max_length=256)
    timeout: float = Field(default=1.0, gt=0, le=10)

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, ports: list[int]) -> list[int]:
        return _validated_ports(ports)


class PortProbeResponse(BaseModel):
    port: int
    state: str
    latency_ms: float


class SubnetScanRequest(BaseModel):
    network: str
    timeout: float = Field(default=0.5, gt=0, le=10)
    ports: list[int] = Field(
        default_factory=lambda: [502, 2404, 48898],
        min_length=1,
        max_length=64,
    )

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, ports: list[int]) -> list[int]:
        return _validated_ports(ports)


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


class QualityChannelResponse(BaseModel):
    object: str
    source: str
    protocol: str
    state: str
    target: str
    latency_ms: float | None = None
    timeouts: int
    reconnects: int
    issue: str | None = None


class QualityMetricResponse(BaseModel):
    key: str
    label: str
    value: float | int
    hint: str
    status: str


class QualityDimensionResponse(BaseModel):
    key: str
    dimension: str
    status: str
    metric: str
    detail: str


class QualityIssueResponse(BaseModel):
    level: str
    object: str
    kind: str
    dimension: str
    issue: str
    duration_seconds: float | None = None
    error: str | None = None


class CommunicationEventResponse(BaseModel):
    timestamp: datetime
    object: str
    event: str
    state: str
    evidence: str


class QualityResponse(BaseModel):
    window: str
    sampled_from: datetime
    sampled_to: datetime
    acquisition_channels: list[QualityChannelResponse]
    delivery_channels: list[QualityChannelResponse]
    channel_summary: list[QualityMetricResponse]
    data_metrics: list[QualityMetricResponse]
    dimensions: list[QualityDimensionResponse]
    issues: list[QualityIssueResponse]
    events: list[CommunicationEventResponse]


class LogEntryResponse(BaseModel):
    timestamp: datetime
    level: str
    source: str
    object: str
    message: str


class LogPageResponse(BaseModel):
    items: list[LogEntryResponse]
    page: PageMeta


class HealthRiskResponse(BaseModel):
    name: str
    state: str
    summary: str
    detail: str


class StorageMountResponse(BaseModel):
    mount: str
    used_gb: float
    total_gb: float
    free_gb: float
    usage_pct: float
    growth_24h_gb: float | None = None
    estimated_full_days: float | None = None


class ResourceSeriesResponse(BaseModel):
    timestamps: list[datetime]
    memory_host_gb: list[float | None]
    memory_rss_gb: list[float | None]
    cpu_host_pct: list[float | None]
    cpu_process_pct: list[float | None]
    cpu_temp_c: list[float | None]
    disk_free_gb: list[float | None]


class SystemHealthResponse(BaseModel):
    range: str
    sampled_at: datetime
    uptime_seconds: float
    cpu_count: int
    load_average: tuple[float, float, float] | None
    risks: list[HealthRiskResponse]
    mounts: list[StorageMountResponse]
    series: ResourceSeriesResponse
    current: dict[str, float | int | str | None]


class AdminDeviceItemRequest(BaseModel):
    device_id: str
    model: str
    device_group: str | None = None
    host: str
    port: int | None = Field(default=None, ge=1, le=65535)
    extensions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AdminTaskItemRequest(BaseModel):
    task_id: str
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float | None = Field(default=None, gt=0)
    sinks: list[str] = Field(default_factory=list)
    enabled: bool = True


class AdminSinkItemRequest(BaseModel):
    name: str
    type: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class AdminDefinitionsStateRequest(BaseModel):
    units: dict[str, dict[str, Any]]
    device_types: dict[str, dict[str, Any]]
    device_models: dict[str, dict[str, Any]]
    point_tables: dict[str, dict[str, Any]]


def _validated_ports(ports: list[int]) -> list[int]:
    """校验端口范围并去重，保持请求顺序。"""
    if any(port < 1 or port > 65535 for port in ports):
        raise ValueError("ports must be between 1 and 65535")
    return list(dict.fromkeys(ports))


def _ensure_unique(values: Iterable[str], label: str) -> None:
    """校验结构化批量请求的业务主键唯一。"""
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError(f"duplicate {label}")


class AdminStateRequest(BaseModel):
    devices: list[AdminDeviceItemRequest]
    tasks: list[AdminTaskItemRequest]
    sinks: list[AdminSinkItemRequest]
    definitions: AdminDefinitionsStateRequest

    @model_validator(mode="after")
    def _validate_state(self) -> AdminStateRequest:
        _ensure_unique((item.device_id for item in self.devices), "device_id")
        _ensure_unique((item.task_id for item in self.tasks), "task_id")
        _ensure_unique((item.name for item in self.sinks), "sink name")
        for item in self.tasks:
            if (item.device is None) == (item.device_group is None):
                raise ValueError(
                    f"task '{item.task_id}' requires exactly one of device/device_group"
                )
        return self
