"""Server 控制面 Worker 静态定义。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class WorkerRole(StrEnum):
    """Worker 角色。"""

    COLLECTOR = "collector"
    COMMANDER = "commander"


class WorkerCapability(StrEnum):
    """Server 可委托给 Worker 的控制面能力。"""

    CONFIG = "config"
    TASK_RUNTIME = "task_runtime"
    ACQUISITION_STATUS = "acquisition_status"
    SINK = "sink"
    METRICS = "metrics"
    DEVICE_IO = "device_io"
    DIAGNOSTICS = "diagnostics"


class WorkerDefinition(BaseModel):
    """组合根登记的 Worker 静态定义。"""

    worker_id: str
    role: WorkerRole
    endpoint: str
    capabilities: list[WorkerCapability]


COLLECTOR_WORKER_ID = "collector"
COMMANDER_WORKER_ID = "commander"
