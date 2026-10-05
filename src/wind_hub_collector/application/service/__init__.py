"""Collector application services。

Collector 只公开运行控制所需的 Query / Task / Config。
Server 侧配置管理、Overview、Quality、Diagnostics 等不从这里聚合导出。
"""

from wind_hub_collector.application.service.config import CollectorConfigService
from wind_hub_collector.application.service.query import (
    AcquisitionInfo,
    CollectorQueryService,
    SystemStatus,
)
from wind_hub_collector.application.service.task import (
    CollectorTaskService,
    TaskInstanceDetail,
    TaskSummary,
)

__all__ = [
    "AcquisitionInfo",
    "CollectorConfigService",
    "CollectorQueryService",
    "CollectorTaskService",
    "SystemStatus",
    "TaskInstanceDetail",
    "TaskSummary",
]
