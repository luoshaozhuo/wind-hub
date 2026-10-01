"""Application use cases——完成完整应用业务流程的编排类。

Inbound adapter（CLI / Web API / IEC104 slave）直接依赖这里的具体
Use Case；不再使用 ``Service`` 命名，也不为单一实现叠加 inbound
port interface。
"""

from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub.application.usecase.config_admin import ConfigAdminUseCase
from wind_hub.application.usecase.definitions import DefinitionsUseCase
from wind_hub.application.usecase.diagnostic import DiagnosticUseCase
from wind_hub.application.usecase.device import DeviceSnapshot, DeviceUseCase
from wind_hub.application.usecase.device_control import DeviceCommandResult, DeviceControlUseCase
from wind_hub.application.usecase.device_data import DeviceDataItem, DeviceDataUseCase, TrendSeries
from wind_hub.application.usecase.overview import OverviewSnapshot, OverviewUseCase
from wind_hub.application.usecase.settings import SettingsUseCase
from wind_hub.application.usecase.sink import SinkUseCase
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
    "DeviceCommandResult",
    "DeviceControlUseCase",
    "DeviceDataItem",
    "DeviceDataUseCase",
    "DeviceSnapshot",
    "DeviceUseCase",
    "ConfigAdminUseCase",
    "ConfigUseCase",
    "DefinitionsUseCase",
    "DiagnosticUseCase",
    "OverviewSnapshot",
    "OverviewUseCase",
    "QueryUseCase",
    "SettingsUseCase",
    "SinkUseCase",
    "SystemStatus",
    "TaskBatchResult",
    "TaskDetail",
    "TaskInstanceDetail",
    "TaskSummary",
    "TaskUseCase",
    "TrendSeries",
    "compute_diff",
]
