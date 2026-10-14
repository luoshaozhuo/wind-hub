"""Worker 静态定义与控制面状态注册表。"""

from wind_hub_server.application.worker.model import (
    COLLECTOR_WORKER_ID,
    COMMANDER_WORKER_ID,
    WorkerCapability,
    WorkerDefinition,
    WorkerRole,
)
from wind_hub_server.application.worker.registry import (
    WorkerRecord,
    WorkerRegistry,
    WorkerState,
)

__all__ = [
    "COLLECTOR_WORKER_ID",
    "COMMANDER_WORKER_ID",
    "WorkerCapability",
    "WorkerDefinition",
    "WorkerRecord",
    "WorkerRegistry",
    "WorkerRole",
    "WorkerState",
]
