"""wind-hub-server composition root.

Server 是独立管理/控制面：不装配、不启动 Collector Runtime。设备即时操作经
Commander gRPC，采集 Task/运行态/Sink 状态经 Collector gRPC；Server 只持有
配置管理、读模型、监控历史与 Web/Admin 用例。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wind_hub_server.adapter.outbound.collector_directory import StaticCollectorDirectory
from wind_hub_server.adapter.outbound.grpc.collector import CollectorGrpcClient
from wind_hub_server.adapter.outbound.grpc.commander import CommanderGrpcClient
from wind_hub_server.application.app_context import AppContext
from wind_hub_server.application.operation import OperationManager
from wind_hub_server.application.port.worker import CollectorPort
from wind_hub_server.application.usecase.admin_state import AdminStateUseCase
from wind_hub_server.application.usecase.collector_aggregate import CollectorAggregateUseCase
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_server.application.usecase.config_admin import ConfigAdminUseCase
from wind_hub_server.application.usecase.definitions import DefinitionsUseCase
from wind_hub_server.application.usecase.device import DeviceUseCase
from wind_hub_server.application.usecase.device_control import DeviceControlUseCase
from wind_hub_server.application.usecase.device_data import DeviceDataUseCase
from wind_hub_server.application.usecase.diagnostic import DiagnosticUseCase
from wind_hub_server.application.usecase.logs import LogsUseCase
from wind_hub_server.application.usecase.overview import OverviewUseCase
from wind_hub_server.application.usecase.quality import QualityUseCase
from wind_hub_server.application.usecase.settings import SettingsUseCase
from wind_hub_server.application.usecase.sink import SinkUseCase
from wind_hub_server.application.usecase.system_health import SystemHealthUseCase
from wind_hub_server.application.usecase.task_assignment import TaskAssignmentUseCase
from wind_hub_server.application.usecase.worker_query import WorkerQueryUseCase
from wind_hub_server.application.usecase.worker_registry import WorkerRegistryUseCase
from wind_hub_server.application.usecase.worker_tasks import CollectorTaskUseCase
from wind_hub_server.application.worker_model import (
    COMMANDER_WORKER_ID,
    WorkerCapability,
    WorkerDefinition,
    WorkerRole,
)
from wind_hub_server.infra.log_store import LogStore
from wind_hub_server.infra.monitoring import MonitoringMetrics, MonitoringService
from wind_hub_server.infra.point_store import InMemoryLatestPointStore, InMemoryTrendStore


@dataclass(slots=True)
class ServerRuntime:
    """Server 独立对象图。"""

    context: AppContext
    config: ConfigUseCase
    monitoring: MonitoringService
    log_store: LogStore
    collector_clients: dict[str, CollectorGrpcClient]
    commander_client: CommanderGrpcClient
    worker_registry: WorkerRegistryUseCase
    collector_directory: StaticCollectorDirectory
    task_assignments: TaskAssignmentUseCase
    tasks: CollectorTaskUseCase


def assemble_server(
    config_dir: str | Path,
    *,
    collectors: dict[str, str],
    commander: str,
) -> ServerRuntime:
    """装配独立 Server，不创建任何 Collector/Commander Runtime。"""
    endpoints = dict(collectors)
    if not endpoints:
        raise ValueError("collectors must not be empty")
    collector_clients = {
        worker_id: CollectorGrpcClient(endpoint)
        for worker_id, endpoint in endpoints.items()
    }
    commander_client = CommanderGrpcClient(commander)

    latest = InMemoryLatestPointStore()
    trend = InMemoryTrendStore(max_samples_per_point=3600)
    monitoring_metrics = MonitoringMetrics()
    log_store = LogStore(capacity=2000)

    startup_config = ConfigUseCase.load_directory(config_dir)

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
    config = ConfigUseCase(
        config_dir=config_dir,
        collectors=collector_directory,
        commander=commander_client,
        current_config=startup_config,
    )
    worker_registry = WorkerRegistryUseCase(
        collector_directory,
        commander_client,
        definitions=[*collector_definitions, commander_definition],
    )
    task_assignments = TaskAssignmentUseCase(config, collector_directory)
    collector_aggregate = CollectorAggregateUseCase(
        collector_directory,
        task_assignments,
        config,
    )
    worker_query = WorkerQueryUseCase(collector_aggregate, commander_client)
    worker_tasks = CollectorTaskUseCase(
        collector_directory,
        task_assignments,
        config,
    )
    devices = DeviceUseCase(collector_aggregate, config)
    device_data = DeviceDataUseCase(
        config,
        collector_aggregate,
    )
    device_control = DeviceControlUseCase(
        commander_client,
        latest,
        trend,
    )
    overview = OverviewUseCase(
        query=worker_query,
        tasks=worker_tasks,
        config=config,
    )
    operations = OperationManager()
    config_admin = ConfigAdminUseCase(config)
    admin_state = AdminStateUseCase(config_admin)
    settings = SettingsUseCase(config, config_admin)
    definitions = DefinitionsUseCase(config, config_admin)
    sink_ops = SinkUseCase(
        collector_directory,
        collector_aggregate,
        task_assignments,
        config,
        config_admin,
    )
    diagnostics = DiagnosticUseCase(
        commander_client,
        device_control,
        config,
        operations,
    )
    monitoring = MonitoringService(
        collector_aggregate,
        monitoring_metrics,
    )
    quality = QualityUseCase(
        config,
        monitoring_metrics,
        monitoring,
    )
    logs = LogsUseCase(log_store)
    system_health = SystemHealthUseCase(monitoring)

    context = AppContext(
        config=config,
        tasks=worker_tasks,
        query=worker_query,
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
    return ServerRuntime(
        context=context,
        config=config,
        monitoring=monitoring,
        log_store=log_store,
        collector_clients=collector_clients,
        commander_client=commander_client,
        worker_registry=worker_registry,
        collector_directory=collector_directory,
        task_assignments=task_assignments,
        tasks=worker_tasks,
    )
