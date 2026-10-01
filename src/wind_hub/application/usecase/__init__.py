"""Application use cases——完成完整应用业务流程的编排类。

Inbound adapter（CLI / Web API / IEC104 slave）直接依赖这里的具体
Use Case；不再使用 ``Service`` 命名，也不为单一实现叠加 inbound
port interface。
"""

from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub.application.usecase.device import DeviceSnapshot, DeviceUseCase
from wind_hub.application.usecase.overview import OverviewSnapshot, OverviewUseCase
from wind_hub.application.usecase.query import AcquisitionInfo, QueryUseCase, SystemStatus
from wind_hub.application.usecase.task import (
    TaskBatchResult,
    TaskDetail,
    TaskInstanceDetail,
    TaskSummary,
    TaskUseCase,
)

__all__ = [
    "AcquisitionInfo",
    "CommandUseCase",
    "DeviceSnapshot",
    "DeviceUseCase",
    "ConfigUseCase",
    "OverviewSnapshot",
    "OverviewUseCase",
    "QueryUseCase",
    "SystemStatus",
    "TaskBatchResult",
    "TaskDetail",
    "TaskInstanceDetail",
    "TaskSummary",
    "TaskUseCase",
    "compute_diff",
]
