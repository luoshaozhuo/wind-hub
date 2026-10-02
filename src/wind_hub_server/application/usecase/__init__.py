"""Server-owned management use cases."""

from wind_hub_server.application.usecase.admin_state import AdminStateUseCase
from wind_hub_server.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub_server.application.usecase.config_admin import ConfigAdminUseCase
from wind_hub_server.application.usecase.definitions import DefinitionsUseCase
from wind_hub_server.application.usecase.device import DeviceSnapshot, DeviceUseCase
from wind_hub_server.application.usecase.device_control import (
    DeviceCommandResult,
    DeviceControlUseCase,
)
from wind_hub_server.application.usecase.device_data import (
    DeviceDataItem,
    DeviceDataUseCase,
    TrendSeries,
)
from wind_hub_server.application.usecase.diagnostic import DiagnosticUseCase
from wind_hub_server.application.usecase.logs import LogsUseCase
from wind_hub_server.application.usecase.overview import (
    OverviewSnapshot,
    OverviewUseCase,
)
from wind_hub_server.application.usecase.quality import QualityUseCase
from wind_hub_server.application.usecase.settings import SettingsUseCase
from wind_hub_server.application.usecase.sink import SinkUseCase
from wind_hub_server.application.usecase.system_health import SystemHealthUseCase

__all__ = [
    "AdminStateUseCase",
    "ConfigAdminUseCase",
    "ConfigUseCase",
    "DefinitionsUseCase",
    "DeviceCommandResult",
    "DeviceControlUseCase",
    "DeviceDataItem",
    "DeviceDataUseCase",
    "DeviceSnapshot",
    "DeviceUseCase",
    "DiagnosticUseCase",
    "LogsUseCase",
    "OverviewSnapshot",
    "OverviewUseCase",
    "QualityUseCase",
    "SettingsUseCase",
    "SinkUseCase",
    "SystemHealthUseCase",
    "TrendSeries",
    "compute_diff",
]
