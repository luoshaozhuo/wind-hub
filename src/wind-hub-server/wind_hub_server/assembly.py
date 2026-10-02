"""wind-hub-server composition root.

Server-specific Admin/Web/Quality/Config objects live here.  The current
transition keeps an embedded Collector core only as a compatibility backend;
the ownership boundary is now explicit so it can later be replaced by gRPC
Worker clients without putting Server concerns back into Collector assembly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wind_hub.adapter.outbound.sink.db.postgres import DBSink
from wind_hub.adapter.outbound.sink.file.csv import FileSink
from wind_hub.adapter.outbound.sink.mq.kafka import KafkaSink
from wind_hub.application.port.sink import SinkPort
from wind_hub.assembly import AssembledRuntime as CollectorRuntime
from wind_hub.assembly import assemble as assemble_collector
from wind_hub.config.schema import SinkConfig
from wind_hub_core.model.errors import ConfigError
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


def assemble_server(config_dir: str | Path) -> ServerRuntime:
    """Build Server-owned management/read-model objects around Collector core."""
    collector = assemble_collector(config_dir)

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
    devices = DeviceUseCase(collector.runtime, config)
    device_data = DeviceDataUseCase(
        collector.runtime,
        config,
        latest,
        trend,
    )
    device_control = DeviceControlUseCase(
        collector.command,
        collector.query,
        latest,
        trend,
    )
    overview = OverviewUseCase(
        query=collector.query,
        tasks=collector.tasks,
        config=config,
    )
    operations = OperationManager()
    config_admin = ConfigAdminUseCase(config)
    admin_state = AdminStateUseCase(config_admin)
    settings = SettingsUseCase(config, config_admin)
    definitions = DefinitionsUseCase(config, config_admin)
    sink_ops = SinkUseCase(
        collector.runtime,
        config,
        config_admin,
        _create_sink,
    )
    diagnostics = DiagnosticUseCase(
        collector.runtime,
        collector.query,
        device_control,
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
        tasks=collector.tasks,
        runtime=collector.runtime,
        command=collector.command,
        query=collector.query,
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
    )


def _create_sink(cfg: SinkConfig) -> SinkPort:
    if cfg.type == "kafka":
        return KafkaSink(cfg)
    if cfg.type == "file":
        return FileSink(cfg)
    if cfg.type == "db":
        return DBSink(cfg)
    raise ConfigError(
        f"Unknown sink type '{cfg.type}' (available: kafka, file, db)"
    )
