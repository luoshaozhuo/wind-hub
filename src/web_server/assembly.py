"""wind-hub-server composition root.

Server 是独立管理/控制面：不装配、不启动 Collector Runtime。设备即时操作经
Commander gRPC，采集 Task/运行态/Sink 状态经 Collector gRPC；Server 只持有
配置管理、读模型、监控历史与 Web/Admin 服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wind_hub_server.adapter.outbound.collector_directory import StaticCollectorDirectory
from wind_hub_server.adapter.outbound.grpc.collector import CollectorGrpcClient
from wind_hub_server.adapter.outbound.grpc.commander import CommanderGrpcClient
from wind_hub_server.application.app_context import AppContext
from wind_hub_server.application.config.admin_state import AdminStateService
from wind_hub_server.application.config.definitions import DefinitionQueryService
from wind_hub_server.application.config.files import ConfigFileService
from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.config.settings import SettingsService
from wind_hub_server.application.device.command import DeviceCommandService
from wind_hub_server.application.device.data import DeviceDataService
from wind_hub_server.application.device.diagnostic import DiagnosticService
from wind_hub_server.application.device.query import DeviceQueryService
from wind_hub_server.application.monitoring.aggregate import CollectorStatusAggregator
from wind_hub_server.application.monitoring.health import SystemHealthService
from wind_hub_server.application.monitoring.logs import LogQueryService
from wind_hub_server.application.monitoring.overview import OverviewService
from wind_hub_server.application.monitoring.quality import QualityService
from wind_hub_server.application.operation.registry import OperationRegistry
from wind_hub_server.application.port.worker import CollectorPort
from wind_hub_server.application.sink.service import SinkService
from wind_hub_server.application.task.control import TaskControlService
from wind_hub_server.application.task.placement import TaskPlacementRegistry
from wind_hub_server.application.task.reconcile import TaskPlacementReconciler
from wind_hub_server.application.worker.model import (
    COMMANDER_WORKER_ID,
    WorkerCapability,
    WorkerDefinition,
    WorkerRole,
)
from wind_hub_server.application.worker.registry import WorkerRegistry
from wind_hub_server.infra.log_store import LogStore
from wind_hub_server.infra.monitoring import MonitoringMetrics, MonitoringService
from wind_hub_server.infra.network_probe import NetworkProbe
from wind_hub_server.infra.point_store import InMemoryTrendStore


@dataclass(slots=True)
class ServerApp:
    """Server 独立对象图。"""

    context: AppContext
    config: ConfigService
    monitoring: MonitoringService
    log_store: LogStore
    collector_clients: dict[str, CollectorGrpcClient]
    commander_client: CommanderGrpcClient
    worker_registry: WorkerRegistry
    collector_directory: StaticCollectorDirectory
    task_placements: TaskPlacementRegistry
    task_reconciler: TaskPlacementReconciler
    tasks: TaskControlService


def assemble_server(
    config_dir: str | Path,
    *,
    collectors: dict[str, str],
    commander: str,
) -> ServerApp:
    """装配独立 Server，不创建任何 Collector/Commander Runtime。"""
    endpoints = dict(collectors)
    if not endpoints:
        raise ValueError("collectors must not be empty")
    collector_clients = {
        worker_id: CollectorGrpcClient(endpoint)
        for worker_id, endpoint in endpoints.items()
    }
    commander_client = CommanderGrpcClient(commander)

    trend = InMemoryTrendStore(max_samples_per_point=3600)
    monitoring_metrics = MonitoringMetrics()
    log_store = LogStore(capacity=2000)

    startup_config = ConfigService.load_directory(config_dir)

    collector_definitions = [
        WorkerDefinition(
            worker_id=worker_id,
            role=WorkerRole.COLLECTOR,
            endpoint=endpoint,
            capabilities=[
                WorkerCapability.CONFIG,
                WorkerCapability.TASK_RUNTIME,
                WorkerCapability.ACQUISITION_STATUS,
                WorkerCapability.SINK,
                WorkerCapability.METRICS,
            ],
        )
        for worker_id, endpoint in endpoints.items()
    ]
    commander_definition = WorkerDefinition(
        worker_id=COMMANDER_WORKER_ID,
        role=WorkerRole.COMMANDER,
        endpoint=commander,
        capabilities=[
            WorkerCapability.CONFIG,
            WorkerCapability.DEVICE_IO,
            WorkerCapability.DIAGNOSTICS,
        ],
    )
    # Directory 面向端口协议；collector_clients 保留具体类型以便 close() 生命周期管理。
    collector_ports: dict[str, CollectorPort] = dict(collector_clients)
    collector_directory = StaticCollectorDirectory(collector_ports)
    config = ConfigService(
        config_dir=config_dir,
        collectors=collector_directory,
        commander=commander_client,
        current_config=startup_config,
    )
    worker_registry = WorkerRegistry(
        collector_directory,
        commander_client,
        definitions=[*collector_definitions, commander_definition],
    )
    task_placements = TaskPlacementRegistry(config, collector_directory)
    collector_status = CollectorStatusAggregator(
        collector_directory,
        task_placements,
        config,
    )
    monitoring = MonitoringService(
        collector_status,
        monitoring_metrics,
    )
    task_reconciler = TaskPlacementReconciler(
        collector_directory,
        task_placements,
        config,
    )
    task_control = TaskControlService(
        collector_directory,
        task_placements,
        task_reconciler,
        config,
        monitoring,
    )
    devices = DeviceQueryService(monitoring, config)
    device_data = DeviceDataService(
        config,
        commander_client,
        trend,
    )
    device_control = DeviceCommandService(
        commander_client,
        trend,
    )
    overview = OverviewService(
        monitoring=monitoring,
        tasks=task_control,
        config=config,
    )
    operations = OperationRegistry()
    config_admin = ConfigFileService(config)
    admin_state = AdminStateService(config_admin)
    settings = SettingsService(config, config_admin)
    definitions = DefinitionQueryService(config, config_admin)
    sink_ops = SinkService(
        collector_directory,
        monitoring,
        task_placements,
        config,
        config_admin,
    )
    diagnostics = DiagnosticService(
        commander_client,
        device_control,
        config,
        operations,
        NetworkProbe(),
    )
    quality = QualityService(
        config,
        monitoring,
    )
    logs = LogQueryService(log_store)
    system_health = SystemHealthService(monitoring)

    context = AppContext(
        config=config,
        tasks=task_control,
        monitoring=monitoring,
        devices=devices,
        device_data=device_data,
        device_control=device_control,
        overview=overview,
        operations=operations,
        admin_state=admin_state,
        config_admin=config_admin,
        settings=settings,
        definitions=definitions,
        sinks=sink_ops,
        diagnostics=diagnostics,
        quality=quality,
        logs=logs,
        system_health=system_health,
        workers=worker_registry,
    )
    return ServerApp(
        context=context,
        config=config,
        monitoring=monitoring,
        log_store=log_store,
        collector_clients=collector_clients,
        commander_client=commander_client,
        worker_registry=worker_registry,
        collector_directory=collector_directory,
        task_placements=task_placements,
        task_reconciler=task_reconciler,
        tasks=task_control,
    )
