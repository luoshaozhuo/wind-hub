"""wind-hub-server composition root.

Server-specific Admin/Web/Quality/Config objects live here.  The current
transition keeps an embedded Collector core only as a compatibility backend;
the ownership boundary is now explicit so it can later be replaced by gRPC
Worker clients without putting Server concerns back into Collector assembly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wind_hub.assembly import AssembledRuntime as CollectorRuntime
from wind_hub.assembly import assemble as assemble_collector
from wind_hub_server.application.app_context import AppContext
from wind_hub_server.application.operation import OperationManager
from wind_hub_server.application.usecase.admin_state import AdminStateUseCase
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
from wind_hub_server.application.usecase.worker_query import WorkerQueryUseCase
from wind_hub_server.application.usecase.worker_tasks import CollectorTaskUseCase
from wind_hub_server.adapter.outbound.grpc.collector import CollectorGrpcClient
from wind_hub_server.adapter.outbound.grpc.commander import CommanderGrpcClient
from wind_hub_server.infra.log_store import LogStore
from wind_hub_server.infra.monitoring import (
    CompositeRuntimeMetrics,
    MonitoringMetrics,
    MonitoringService,
)
from wind_hub_server.infra.point_store import (
    InMemoryLatestPointStore,
    InMemoryTrendStore,
)
from wind_hub_server.infra import metrics


@dataclass(slots=True)
class ServerRuntime:
    """Server composition result."""

    collector: CollectorRuntime
    context: AppContext
    config: ConfigUseCase
    monitoring: MonitoringService
    log_store: LogStore
    collector_client: CollectorGrpcClient
    commander_client: CommanderGrpcClient


def assemble_server(
    config_dir: str | Path,
    *,
    collector_target: str = "127.0.0.1:50051",
    commander_target: str = "127.0.0.1:50052",
) -> ServerRuntime:
    """Build Server-owned management/read-model objects and Worker clients."""
    collector = assemble_collector(config_dir)
    collector_client = CollectorGrpcClient(collector_target)
    commander_client = CommanderGrpcClient(commander_target)

    latest = InMemoryLatestPointStore()
    trend = InMemoryTrendStore(max_samples_per_point=3600)
    monitoring_metrics = MonitoringMetrics()
    log_store = LogStore(capacity=2000)

    collector.engine.add_observer(latest.put_batch)
    collector.engine.add_observer(trend.append_batch)
    collector.engine.add_observer(monitoring_metrics.observe_points)
    collector.runtime.attach_metrics_hook(
        CompositeRuntimeMetrics(
            metrics.PrometheusRuntimeMetrics(),
            monitoring_metrics,
        )
    )

    config = ConfigUseCase(
        config_dir=config_dir,
        runtime=collector.runtime,
        current_config=collector.boot_config,
    )
    worker_query = WorkerQueryUseCase(collector_client, commander_client)
    worker_tasks = CollectorTaskUseCase(collector_client)
    devices = DeviceUseCase(collector_client, config)
    device_data = DeviceDataUseCase(
        config,
        latest,
        trend,
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
        collector_client,
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
        collector.runtime,
        monitoring_metrics,
    )
    quality = QualityUseCase(
        collector.runtime,
        config,
        monitoring_metrics,
        monitoring,
    )
    logs = LogsUseCase(log_store)
    system_health = SystemHealthUseCase(monitoring)

    context = AppContext(
        config=config,
        tasks=worker_tasks,
        runtime=collector.runtime,
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
    )
    return ServerRuntime(
        collector=collector,
        context=context,
        config=config,
        monitoring=monitoring,
        log_store=log_store,
        collector_client=collector_client,
        commander_client=commander_client,
    )


