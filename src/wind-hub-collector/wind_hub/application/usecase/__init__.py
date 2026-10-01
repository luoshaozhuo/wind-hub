"""Collector application use cases。

Collector 只公开运行控制所需的 Command / Query / Task。
Server 侧配置管理、Overview、Quality、Diagnostics 等不从这里聚合导出。
"""

from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.application.usecase.query import (
    AcquisitionInfo,
    QueryUseCase,
    SystemStatus,
)
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
    "QueryUseCase",
    "SystemStatus",
    "TaskBatchResult",
    "TaskDetail",
    "TaskInstanceDetail",
    "TaskSummary",
    "TaskUseCase",
]
