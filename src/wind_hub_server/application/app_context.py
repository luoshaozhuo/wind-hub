"""Server 进程级 application context。

``AppContext`` 由组合根装配后注入 Web API inbound adapter。上下文只持有
Server 应用层服务/注册表/端口，不暴露 Collector 或 Commander Runtime 对象。
缺失上下文由 Web API 统一映射为 HTTP 503。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from wind_hub_server.application.config.admin_state import AdminStateService
from wind_hub_server.application.config.definitions import DefinitionQueryService
from wind_hub_server.application.config.files import ConfigFileService
from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.config.settings import SettingsService
from wind_hub_server.application.device.command import DeviceCommandService
from wind_hub_server.application.device.data import DeviceDataService
from wind_hub_server.application.device.diagnostic import DiagnosticService
from wind_hub_server.application.device.query import DeviceQueryService
from wind_hub_server.application.monitoring.health import SystemHealthService
from wind_hub_server.application.monitoring.logs import LogQueryService
from wind_hub_server.application.monitoring.overview import OverviewService
from wind_hub_server.application.monitoring.quality import QualityService
from wind_hub_server.application.operation.registry import OperationRegistry
from wind_hub_server.application.port.monitoring import MonitoringSnapshotPort
from wind_hub_server.application.sink.service import SinkService
from wind_hub_server.application.task.control import TaskControlService
from wind_hub_server.application.worker.registry import WorkerRegistry


@dataclass
class AppContext:
    """进程级共享 application context——应用层服务与 Runtime 的依赖容器。

    所有字段都可选：缺失服务由适配器上报 503 / 非零退出而非崩溃。

    三类启停语义在本容器中各有归属：Runtime 生命周期经 ``runtime``
    （组合根/进程入口编排），采集 Task Instance 生命周期经 ``tasks``，
    进程生命周期由 ``main.py`` 信号处理负责。
    """

    monitoring: MonitoringSnapshotPort | None = None
    """Server 低频运行态/设备/Sink/历史监控事实源。"""

    config: ConfigService | None = None
    """可选配置服务（热重载）。引擎可能不带配置服务运行（如只读部署），
    缺失时由适配器上报 503 / 非零退出而非崩溃。"""

    tasks: TaskControlService | None = None
    """采集 Task/Task Instance 查询与显式 start/stop 服务。"""

    devices: DeviceQueryService | None = None
    """V1 设备查询服务；聚合静态配置与实时连接状态。"""

    device_data: DeviceDataService | None = None
    """V1 Devices Data / Trend 缓存查询服务。"""

    device_control: DeviceCommandService | None = None
    """V1 设备写控制与回读服务。"""

    overview: OverviewService | None = None
    """V1 Overview 聚合只读模型。"""

    operations: OperationRegistry | None = None
    """进程内异步 Operation 注册表。"""

    admin_state: AdminStateService | None = None
    """前端结构化配置批量写服务。"""

    config_admin: ConfigFileService | None = None
    """配置文件管理/历史服务。"""

    settings: SettingsService | None = None
    """System Settings 服务。"""

    definitions: DefinitionQueryService | None = None
    """Definitions 聚合查询服务。"""

    sinks: SinkService | None = None
    """Sink 管理与测试服务。"""

    diagnostics: DiagnosticService | None = None
    """网络/协议诊断服务。"""

    quality: QualityService | None = None
    """采集/交付质量聚合服务。"""

    logs: LogQueryService | None = None
    """结构化进程日志查询服务。"""

    system_health: SystemHealthService | None = None
    """宿主机/进程资源健康服务。"""

    workers: WorkerRegistry | None = None
    """Worker Registry 只读状态与探测。"""


_context: AppContext | None = None
_lock = threading.Lock()


def set_context(ctx: AppContext) -> None:
    """Set the global application context (called by ``main.py`` or tests)."""
    global _context
    with _lock:
        _context = ctx


def get_context() -> AppContext:
    """Return the global application context.

    Raises:
        RuntimeError: If no context has been set yet.
    """
    with _lock:
        ctx = _context
    if ctx is None:
        raise RuntimeError("AppContext is not set — call set_context() first")
    return ctx


def clear_context() -> None:
    """Clear the global context (used by tests for isolation)."""
    global _context
    with _lock:
        _context = None


__all__ = ["AppContext", "set_context", "get_context", "clear_context"]
