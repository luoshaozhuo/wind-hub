"""Server 到 Collector/Commander 的出站端口与边界 DTO。

端口返回的 DTO 是 application 边界的稳定契约：gRPC outbound adapter 负责
protobuf ↔ DTO 转换，application 内部只做属性访问，不再有魔术字符串键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from core.application.command import Command, CommandResult
from web_server.application.device.sample import PointValue
from web_server.application.port.monitoring import (
    AcquisitionStatus,
    MetricsSnapshot,
)


class CollectorTaskConflictError(RuntimeError):
    """Collector rejected a revision-checked device task operation."""


# ===================================================================
# 配置事务（Commander / Collector 共用同一组 Ack 契约）
# ===================================================================


@dataclass(frozen=True)
class ConfigPrepareAck:
    """Worker 对 PrepareConfig 的应答；Commander 不上报 errors/duration。"""

    success: bool
    revision_id: str
    config_hash: str
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


@dataclass(frozen=True)
class ConfigActivateAck:
    """Worker 对 ActivateConfig 的应答；Commander 不上报 errors/duration。"""

    success: bool
    revision_id: str
    active_config_hash: str
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


@dataclass(frozen=True)
class ConfigAbortAck:
    """Worker 对 AbortConfig 的应答。"""

    success: bool
    revision_id: str
    aborted: bool


# ===================================================================
# Commander 诊断与状态
# ===================================================================


@dataclass(frozen=True)
class CommanderStatus:
    """Commander 运行与配置状态。"""

    running: bool
    device_count: int
    active_revision: str
    active_config_hash: str
    prepared_revision: str | None


@dataclass(frozen=True)
class DiagnosticStage:
    """设备链路验证的单阶段结果。"""

    name: str
    ok: bool
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class DeviceVerifyResult:
    """设备链路验证结果。"""

    device_id: str
    protocol: str
    host: str
    port: int
    ok: bool
    stages: list[DiagnosticStage]


@dataclass(frozen=True)
class PointVerifyResult:
    """单点在线读取验证结果。

    ``configured_address``/``resolved_address`` 是协议相关的动态键值地址，
    保留 dict；``raw_value``/``engineering_value`` 是动态标量。
    """

    device_id: str
    point_id: str
    variable_name: str | None
    protocol: str
    configured_address: dict[str, Any]
    resolved_address: dict[str, Any] | None
    data_type: str
    scale: float
    offset: float
    unit: str
    readable: bool | None
    ok: bool
    code: str
    severity: str
    raw_value: Any
    engineering_value: Any
    quality: str | None
    source: str | None
    error: str | None


# ===================================================================
# Collector 状态与运行快照
# ===================================================================


@dataclass(frozen=True)
class CollectorInfo:
    """Runtime identity from Collector v2, independent of config ownership."""

    collector_id: str
    boot_id: str
    active_revision: str
    runtime_running: bool
    active_config_hash: str | None = None


@dataclass(frozen=True)
class CollectorRuntimeStatus:
    """单 Collector 的 Runtime 聚合状态。"""

    running: bool
    device_count: int
    devices_connected: int
    points_collected: int
    points_routed: int
    points_dropped: int
    acquisitions: list[AcquisitionStatus]


@dataclass(frozen=True)
class DeviceRuntimeInfo:
    """Collector 上报的单设备运行状态。"""

    device_id: str
    protocol: str
    connected: bool
    last_seen: str | None
    consecutive_failures: int
    last_error: str | None


@dataclass(frozen=True)
class CollectorTaskSummary:
    """Collector 上报的 Task Definition 聚合状态。"""

    task_id: str
    device: str | None
    device_group: str | None
    point_group: str
    interval: float | None
    targets: list[str]
    enabled: bool
    runtime_state: str
    instance_count: int
    running_instances: int
    stopped_instances: int
    failed_instances: int


@dataclass(frozen=True)
class CollectorTaskInstance:
    """Collector 上报的单 Task Instance 状态。"""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    interval: float | None
    targets: list[str]
    state: str


class CommanderPort(Protocol):
    """即时设备操作端口。"""

    async def status(self) -> CommanderStatus: ...

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
    ) -> ConfigPrepareAck: ...

    async def activate_config(self, revision_id: str) -> ConfigActivateAck: ...

    async def abort_config(self, revision_id: str) -> ConfigAbortAck: ...

    async def read_point(self, device_id: str, point_id: str) -> PointValue: ...

    async def read_points(
        self,
        device_id: str,
        point_ids: list[str],
    ) -> list[PointValue]: ...

    async def write(self, command: Command) -> CommandResult: ...

    async def verify_device(
        self,
        device_id: str,
        timeout: float = 1.0,
    ) -> DeviceVerifyResult: ...

    async def verify_point(self, device_id: str, point_id: str) -> PointVerifyResult: ...


class CollectorPort(Protocol):
    """Collector v2 read queries and revision-checked device task controls."""

    async def config_status(self) -> CollectorInfo: ...
    async def runtime_status(self) -> CollectorRuntimeStatus: ...
    async def metrics_snapshot(self) -> MetricsSnapshot: ...
    async def list_devices(self) -> list[DeviceRuntimeInfo]: ...
    async def list_tasks(self) -> list[CollectorTaskSummary]: ...
    async def list_task_instances(self) -> list[CollectorTaskInstance]: ...
    async def start_task(self, device_id: str) -> CollectorTaskSummary: ...
    async def stop_task(self, device_id: str) -> CollectorTaskSummary: ...
    async def start_task_instance(self, device_id: str) -> CollectorTaskInstance: ...
    async def stop_task_instance(self, device_id: str) -> CollectorTaskInstance: ...
