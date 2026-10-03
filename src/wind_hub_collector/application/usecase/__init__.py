"""Collector application use cases。

Collector 只公开运行控制所需的 Query / Task / Config。
Server 侧配置管理、Overview、Quality、Diagnostics 等不从这里聚合导出。
"""

from wind_hub_collector.application.usecase.config import ConfigUseCase
from wind_hub_collector.application.usecase.query import (
    AcquisitionInfo,
    QueryUseCase,
    SystemStatus,
)
from wind_hub_collector.application.usecase.task import (
    TaskBatchResult,
    TaskInstanceDetail,
    TaskSummary,
    TaskUseCase,
)

__all__ = [
    "AcquisitionInfo",
    "ConfigUseCase",
    "QueryUseCase",
    "SystemStatus",
    "TaskBatchResult",
    "TaskInstanceDetail",
    "TaskSummary",
    "TaskUseCase",
]
