"""Server 到 Collector/Commander 的出站端口与边界 DTO。

端口返回的 DTO 是 application 边界的稳定契约：gRPC outbound adapter 负责
protobuf ↔ DTO 转换，application 内部只做属性访问，不再有魔术字符串键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from web_server.application.device.command_model import Command, CommandResult
from web_server.application.device.sample import PointValue
from web_server.application.port.monitoring import (
    AcquisitionStatus,
    MetricsSnapshot,
)


class CollectorTaskConflictError(RuntimeError):
    """Collector rejected a revision-checked device task operation."""


# ===================================================================
# Commander 正式快照事务：Disk revision → Candidate ID → Apply
# ===================================================================


@dataclass(frozen=True)
class RemoteConfigSnapshot:
    revision: str
    files: dict[str, bytes]


@dataclass(frozen=True)
class ConfigPushResult:
    candidate_id: str
    valid: bool
    base_revision: str
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigApplyResult:
    file_commit: str
    effective_revision: str
    restart_required: bool
    errors: list[str] = field(default_factory=list)


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


# ===================================================================
# Collector 状态与运行快照
# ===================================================================


@dataclass(frozen=True)
class CollectorInfo:
    """Collector 正式 CommonService 暴露的运行时身份。"""

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
    """正式 Commander v1 控制、快照、诊断边界。"""

    async def status(self) -> CommanderStatus: ...

    async def get_config_snapshot(
        self, scope: str
    ) -> RemoteConfigSnapshot: ...

    async def push_config(
        self, files: dict[str, bytes], expected_revision: str
    ) -> ConfigPushResult: ...

    async def apply_config(
        self, candidate_id: str, expected_revision: str
    ) -> ConfigApplyResult: ...

    async def write(self, command: Command) -> CommandResult: ...

    async def verify_device(
        self, device_id: str, timeout: float = 1.0
    ) -> DeviceVerifyResult: ...


class CollectorPort(Protocol):
    """Collector v1 查询、即时读点和受修订号保护的任务控制。"""

    async def config_status(self) -> CollectorInfo: ...
    async def runtime_status(self) -> CollectorRuntimeStatus: ...
    async def metrics_snapshot(self) -> MetricsSnapshot: ...
    async def read_points(self, device_id: str, point_ids: list[str]) -> list[PointValue]: ...
    async def list_devices(self) -> list[DeviceRuntimeInfo]: ...
    async def list_tasks(self) -> list[CollectorTaskSummary]: ...
    async def list_task_instances(self) -> list[CollectorTaskInstance]: ...
    async def start_task(self, device_id: str) -> CollectorTaskSummary: ...
    async def stop_task(self, device_id: str) -> CollectorTaskSummary: ...
    async def start_task_instance(self, device_id: str) -> CollectorTaskInstance: ...
    async def stop_task_instance(self, device_id: str) -> CollectorTaskInstance: ...
