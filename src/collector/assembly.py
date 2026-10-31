"""Collector 组合根。

只装配采集链路：设备会话（Core 协议 Driver）、采集引擎、NATS Publisher 与
热重载编排；不包含命令分发、诊断或 Web API。组合根是唯一允许跨层引用
Infrastructure 具体实现的地方：它显式注册 Core 协议 Driver、持有进程级
ADS 本机身份。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from contracts.sample_message import validate_subject_token
from core.application import ProtocolRegistry
from core.application.settings import ADSLocalConfig, LogSettings
from core.domain import Device, PointTable
from core.infrastructure.config import CollectorDocumentLoader, ConfigPaths
from core.infrastructure.config.collector_snapshot import build_collector_snapshot
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver

from .application.config import CollectorConfig
from .application.config_service import CollectorConfigService
from .application.identity import CollectorIdentity, build_collector_identity
from .application.latest_values import LatestValues
from .application.metrics_state import CollectorMetricsState
from .application.nats_dispatch import SamplePublisher
from .application.runtime import CollectorRuntime
from .application.session import CollectorDeviceSession
from .domain.acquisition import AcquisitionEngine
from .infrastructure.config.admin import CollectorConfigAdmin
from .infrastructure.config.desired_state_store import DesiredStateStore
from .infrastructure.config.loader import config_from_snapshot_and_documents, load_collector_config
from .infrastructure.config.transactions import CollectorConfigTransactions
from .infrastructure.nats_publisher import NatsPublisher

logger = logging.getLogger(__name__)


def build_protocol_registry() -> ProtocolRegistry:
    """显式注册 Collector 使用的 Core 协议 Driver。"""
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("modbus", ModbusDriver)
    registry.register("iec104", IEC104Driver)
    return registry


@dataclass(slots=True)
class CollectorApp:
    """Collector 进程对象图。

    Attributes:
        boot_config: 进程启动时加载的配置快照；仅表示启动基线。
        runtime: Collector 运行时（Device/Task/Publisher 编排）。
        config: 配置热重载编排服务。
        config_file: Collector 服务配置文件。
        instances: 设备实例目录。
        templates: 版本化模板根目录。
        config_hash: 启动配置集摘要。
        metrics: 进程级采集质量计数与近期事件（gRPC 指标快照的唯一事实源）。
        identity: 本次进程启动身份（collector_id / boot_id / 启动摘要）。
    """

    boot_config: CollectorConfig
    runtime: CollectorRuntime
    config: CollectorConfigService
    config_admin: CollectorConfigAdmin
    config_file: Path
    config_paths: ConfigPaths
    site_id: str
    config_hash: str
    metrics: CollectorMetricsState
    latest_values: LatestValues
    identity: CollectorIdentity
    ads_local: ADSLocalConfig | None
    log_settings: LogSettings
    metric_transport: NatsPublisher
    metric_windows: tuple[int, ...]
    metric_interval: int
    metrics_enabled: bool
    _metrics_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _config_retry_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        """Start runtime and independent failed-config convergence supervision."""
        if self.ads_local is not None:
            await ADSDriver.initialize_local(self.ads_local)
        try:
            await self.runtime.start()
        except BaseException:
            await ADSDriver.close_local()
            raise
        if self.metrics_enabled:
            self._metrics_task = asyncio.create_task(
                self._publish_metrics_loop(), name="collector-metrics-publisher"
            )
        self._config_retry_task = asyncio.create_task(
            self._retry_config_convergence(),
            name="collector-config-retry",
        )

    async def _publish_metrics_loop(self) -> None:
        """Periodically publish rolling windows to an isolated metrics subject."""
        from datetime import UTC, datetime

        subject = f"wh.metrics.{self.site_id}.{self.identity.collector_id}"
        while True:
            await asyncio.sleep(self.metric_interval)
            payload = {
                "schema_version": 1,
                "collector_id": self.identity.collector_id,
                "boot_id": self.identity.boot_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "windows": {
                    str(window): self.metrics.window_snapshot(window)
                    for window in self.metric_windows
                },
                "counters": self.metrics.snapshot()["counters"],
                "nats_connected": self.runtime.nats_connected,
            }
            if not await self.metric_transport.publish_metrics(subject, payload):
                logger.warning("Collector metrics not published; retry next interval")

    async def _retry_config_convergence(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.config_admin.retry_failed()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Collector config convergence retry failed", exc_info=True)

    async def stop(self) -> None:
        """Stop background retries, acquisition, publishing, and ADS identity."""
        metrics_task = self._metrics_task
        self._metrics_task = None
        if metrics_task is not None:
            metrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await metrics_task
        task = self._config_retry_task
        self._config_retry_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await self.runtime.stop()
        finally:
            await ADSDriver.close_local()


def assemble_collector(
    config_file: str | Path,
    instances: str | Path,
    templates: str | Path,
    *,
    collector_id: str | None = None,
) -> CollectorApp:
    """从现场配置目录装配 Collector，不执行网络 I/O。

    Args:
        config_file: Collector 服务配置文件。
        collector_id: 显式 Collector 稳定标识；为空时读取环境变量或主机名。

    Raises:
        ConfigError: 配置非法。
    """
    config_path = Path(config_file)
    config_source = CollectorDocumentLoader(
        ConfigPaths(config_path, Path(instances), Path(templates))
    )
    documents = config_source.load()
    snapshot = build_collector_snapshot(documents)
    config = config_from_snapshot_and_documents(snapshot, documents)
    config_hash = documents.revision

    protocol_registry = build_protocol_registry()

    def session_factory(device: Device, point_table: PointTable) -> CollectorDeviceSession:
        protocol = protocol_registry.create(
            device.endpoint,
            point_table,
            device.protocol_options,
        )
        return CollectorDeviceSession(
            device,
            point_table,
            protocol,
            subscribe_enabled=device.subscribe_enabled,
        )

    devices = {
        str(device_id): session_factory(device, config.point_table_for_device(device_id))
        for device_id, device in config.devices.items()
    }

    transport = NatsPublisher(config.messaging.servers, config.messaging.delivery)
    identity = build_collector_identity(config_hash, collector_id=collector_id)
    validate_subject_token(snapshot.site.site_id)
    validate_subject_token(identity.collector_id)
    for task_id in config.tasks:
        validate_subject_token(task_id)
    for device_id in config.devices:
        validate_subject_token(str(device_id))
        table = config.point_table_for_device(device_id)
        for point_id in table.points:
            validate_subject_token(point_id)

    raw_metrics = documents.service.get("metrics", {})
    publisher = SamplePublisher(
        transport,
        wind_farm_id=snapshot.site.site_id,
        identity=identity,
        delivery=config.messaging.delivery,
        max_staleness_ms=config.messaging.max_staleness_ms,
        queue_capacity=raw_metrics.get("pending_capacity", 2048),
    )
    metrics = CollectorMetricsState(
        event_capacity=raw_metrics.get("event_capacity", 5000),
        windows=tuple(raw_metrics.get("windows", (60, 300, 3600))),
        resolution=raw_metrics.get("publish_interval", 60),
    )
    latest_values = LatestValues()
    engine = AcquisitionEngine(
        read_timeout=config.runtime.read_timeout,
        on_points_collected=metrics.observe_collected,
        on_points_bad=metrics.observe_bad,
    )
    engine.add_observer(latest_values.update)
    intent_store = DesiredStateStore(instances)
    runtime = CollectorRuntime(
        devices,
        publisher,
        engine,
        config.runtime,
        tasks=dict(config.tasks),
        session_factory=session_factory,
        metrics_hook=metrics,
        desired_states={str(key): value for key, value in config.desired_states.items()},
        persist_desired_state=intent_store.set_state,
    )

    config_service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: load_collector_config(config_source),
        config_digest=lambda: config_source.load().revision,
        config_hash=config_hash,
        latest_values=latest_values,
    )

    def authorize_config(selector: object, operation: str) -> None:
        from .infrastructure.config.transactions import ConfigSelector

        if not isinstance(selector, ConfigSelector):
            raise PermissionError("unknown configuration selector")
        if (
            selector.scope == "TEMPLATE"
            and operation == "write"
            and os.getenv("WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH") != "enabled"
        ):
            raise PermissionError("template publication is not authorized")

    config_admin = CollectorConfigAdmin(
        CollectorConfigTransactions(
            ConfigPaths(config_path, Path(instances), Path(templates)),
            documents,
        ),
        config_service,
        authorize=authorize_config,
    )

    return CollectorApp(
        boot_config=config,
        runtime=runtime,
        config=config_service,
        config_admin=config_admin,
        config_file=config_path,
        config_paths=ConfigPaths(config_path, Path(instances), Path(templates)),
        site_id=snapshot.site.site_id,
        config_hash=config_hash,
        metrics=metrics,
        metric_transport=transport,
        metric_windows=tuple(raw_metrics.get("windows", (60, 300, 3600))),
        metric_interval=raw_metrics.get("publish_interval", 60),
        metrics_enabled=raw_metrics.get("enabled", True),
        latest_values=latest_values,
        identity=identity,
        ads_local=config.ads_local,
        log_settings=snapshot.system.logs,
    )


__all__ = [
    "CollectorApp",
    "assemble_collector",
    "build_protocol_registry",
]
