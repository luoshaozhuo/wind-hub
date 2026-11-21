"""Collector v1 composition root for configuration and acquisition."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import grpc
import yaml

from collector.infrastructure.adapters.inbound.grpc.collector_service import CollectorDeviceRpc
from collector.infrastructure.adapters.inbound.grpc.config_service import CollectorConfigRpc
from collector.infrastructure.adapters.inbound.grpc.registration import register_collector_rpc
from collector.infrastructure.adapters.outbound.metrics_emitter import CollectorMetricsEmitter
from collector.infrastructure.adapters.outbound.publisher import CollectorPublisher
from core.application import ProtocolPort, ProtocolRegistry
from core.application.settings import ADSLocalConfig
from core.configuration.local_selection import select_local_service
from core.domain import Device, PointTable
from core.infrastructure.config import read_yaml
from core.infrastructure.configuration_repository import DeploymentObserver, SelectedBundle
from core.infrastructure.executor import configure_default_thread_pool
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver

from .application.acquisition import CollectorAcquisition
from .application.configuration import CollectorConfigurationManager
from .application.lifecycle import CollectorLifecycle
from .application.task_control import CollectorTaskControl
from .application.telemetry import CollectorTelemetry
from .infrastructure.config.service_identity import sources_changed
from .infrastructure.config.snapshot_repository import (
    CollectorSnapshotRepository,
)
from .infrastructure.config.task_intents import SqliteTaskIntentStore
from .infrastructure.config.validation import (
    parse_collector_runtime,
    resolve_source_paths,
    validate_collector_snapshot,
)
from .infrastructure.resource_sampler import CollectorResourceSampler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectorApp:
    lifecycle: CollectorLifecycle
    configuration: CollectorConfigurationManager
    task_control: CollectorTaskControl
    server: grpc.aio.Server
    endpoint: str
    ads_local: ADSLocalConfig | None = None
    thread_pool_workers: int = 32
    metrics_emitter: CollectorMetricsEmitter | None = None
    resource_sampler: CollectorResourceSampler | None = None
    _started: bool = False
    _retry_task: asyncio.Task[None] | None = field(default=None, init=False)
    observer: DeploymentObserver | None = None
    _observer_task: asyncio.Task[None] | None = field(default=None, init=False)
    _observer_stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    async def start(self) -> None:
        if self._started:
            return
        revision = self.configuration.status().disk_revision
        paths = self.configuration.active_paths()
        configure_default_thread_pool(self.thread_pool_workers)
        if self.ads_local is not None:
            await ADSDriver.initialize_local(self.ads_local)
        try:
            await self.lifecycle.start(paths, revision)
        except BaseException:
            if self.ads_local is not None:
                await ADSDriver.close_local()
            raise
        await asyncio.to_thread(self.configuration.mark_started, self.lifecycle.apply_errors)
        try:
            await self.server.start()
        except BaseException:
            try:
                await self.lifecycle.stop()
            finally:
                if self.ads_local is not None:
                    await ADSDriver.close_local()
            raise
        try:
            if self.metrics_emitter is not None:
                await self.metrics_emitter.start()
            if self.resource_sampler is not None:
                await self.resource_sampler.start()
        except BaseException:
            try:
                if self.resource_sampler is not None:
                    await self.resource_sampler.stop()
            finally:
                try:
                    if self.metrics_emitter is not None:
                        await self.metrics_emitter.stop()
                finally:
                    try:
                        await self.server.stop(grace=0)
                    finally:
                        try:
                            await self.lifecycle.stop()
                        finally:
                            if self.ads_local is not None:
                                await ADSDriver.close_local()
            raise
        self._started = True
        self._retry_task = asyncio.create_task(
            self._retry_failed_config(), name="collector-config-convergence"
        )
        if self.observer is not None:
            self._observer_stop.clear()
            self._observer_task = asyncio.create_task(
                self.observer.run(self._observer_stop, self._apply_selected_bundle),
                name="collector-deployment-reconcile",
            )

    async def _apply_selected_bundle(self, bundle: SelectedBundle) -> bool:
        """Commit an authenticated selected closure, then converge safe devices."""
        allowed = {
            name: content
            for name, content in bundle.files.items()
            if name.startswith(("services/", "instances/", "templates/"))
        }
        current_revision, current = await asyncio.to_thread(self.configuration.get_disk)
        if allowed == current:
            return self.configuration.status().converged
        candidate_id, _ = await asyncio.to_thread(
            self.configuration.push, allowed, expected_revision=current_revision
        )
        result = await self.configuration.apply(candidate_id, expected_revision=current_revision)
        return result.converged

    async def _retry_failed_config(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                if self.configuration.status().apply_errors:
                    await self.configuration.retry_failed()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Collector configuration convergence retry failed")

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._observer_stop.set()
        observer_task = self._observer_task
        self._observer_task = None
        if observer_task is not None:
            await observer_task
        task = self._retry_task
        self._retry_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await self.server.stop(grace=5.0)
        finally:
            try:
                await self.lifecycle.stop()
            finally:
                try:
                    if self.resource_sampler is not None:
                        await self.resource_sampler.stop()
                finally:
                    try:
                        if self.metrics_emitter is not None:
                            await self.metrics_emitter.stop()
                    finally:
                        if self.ads_local is not None:
                            await ADSDriver.close_local()


async def assemble_collector(
    config_file: str | Path,
    *,
    state_directory: str | Path,
    collector_id: str | None = None,
    grpc_host: str = "127.0.0.1",
    grpc_port: int = 50051,
) -> CollectorApp:
    """Create a Collector whose configuration comes from one verified generation."""
    # CURRENT is authoritative after the initial import. Never consult the
    # mutable source tree during crash recovery or normal process restart.
    config_path = Path(config_file).absolute()
    if config_path.parent.name != "services":
        raise ValueError("Collector service file must reside in a services directory")
    validate_snapshot = partial(validate_collector_snapshot, source_root=config_path.parent.parent)
    repository = CollectorSnapshotRepository(Path(state_directory))
    if repository.active_revision() is None:
        source = await asyncio.to_thread(resolve_source_paths, config_path)
        files = await asyncio.to_thread(repository.collect_source, source)
        candidate = await asyncio.to_thread(
            repository.stage,
            files,
            expected_revision="",
            validate=validate_snapshot,
        )
        await repository.commit(
            candidate.candidate_id,
            expected_revision="",
            validate=validate_snapshot,
        )
    service = read_yaml(repository.active_paths().service)
    shared = await asyncio.to_thread(
        parse_collector_runtime,
        repository.active_paths(),
        repository.active_revision() or "",
    )
    messaging = service["messaging"]
    telemetry = CollectorTelemetry(
        event_limit=service.get("metrics", {}).get("event_capacity", 1000),
        windows_seconds=tuple(service.get("metrics", {}).get("windows", [60, 300, 3600])),
    )
    metrics_emitter = (
        CollectorMetricsEmitter(
            telemetry,
            servers=messaging["servers"],
            site_id=shared.site_id,
            collector_id=(
                collector_id or os.getenv("WIND_HUB_COLLECTOR_ID") or socket.gethostname()
            ),
            interval_seconds=service.get("metrics", {}).get("publish_interval", 60),
            max_staleness_seconds=service.get("metrics", {}).get("max_staleness", 300),
            capacity=service.get("metrics", {}).get("pending_capacity", 120),
        )
        if service.get("metrics", {}).get("enabled", True)
        else None
    )
    publisher = CollectorPublisher(
        servers=messaging["servers"],
        site_id=shared.site_id,
        collector_id=(collector_id or os.getenv("WIND_HUB_COLLECTOR_ID") or socket.gethostname()),
        delivery=messaging.get("delivery", "realtime"),
        max_staleness_ms=messaging.get("max_staleness_ms", 500),
        max_samples_per_batch=messaging.get(
            "max_samples_per_batch",
            100 if messaging.get("delivery", "realtime") == "realtime" else 500,
        ),
        max_batch_bytes=messaging.get("max_batch_bytes", 1_048_576),
        enqueue_timeout_s=messaging.get("enqueue_timeout_s", 2.0),
        capacity=messaging.get("queue_capacity", 2048),
        # The bounded sample queue is independent of metrics.pending_capacity,
        # which design §11.4 defines as the pending Metrics-message capacity.
        telemetry=telemetry,
    )
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("modbus", ModbusDriver)
    registry.register("iec104", IEC104Driver)

    def create_driver(device: Device, point_table: PointTable) -> ProtocolPort:
        return registry.create(device.endpoint, point_table, device.protocol_options)

    acquisition = CollectorAcquisition(
        create_driver,
        publisher,
        telemetry=telemetry,
        metrics_subject=metrics_emitter,
    )
    intents = SqliteTaskIntentStore(Path(state_directory) / "task_intents.sqlite3")
    lifecycle = CollectorLifecycle(
        partial(parse_collector_runtime, task_intents=intents),
        acquisition,
    )

    def requires_restart(old: bytes, new: bytes) -> bool:
        return bool(yaml.safe_load(old) != yaml.safe_load(new))

    configuration = CollectorConfigurationManager(
        repository,
        validate=validate_snapshot,
        runtime=lifecycle,
        restart_required=requires_restart,
        source_changed=sources_changed,
        telemetry=telemetry,
    )
    if not 0 <= grpc_port <= 65535:
        raise ValueError("grpc_port must be within 0..65535")
    certificate_path = os.getenv("WIND_HUB_COLLECTOR_TLS_CERT")
    key_path = os.getenv("WIND_HUB_COLLECTOR_TLS_KEY")
    ca_path = os.getenv("WIND_HUB_COLLECTOR_TLS_CA")
    tls_configured = all((certificate_path, key_path, ca_path))
    if any((certificate_path, key_path, ca_path)) and not tls_configured:
        raise ValueError("Collector mTLS requires certificate, key and CA together")
    if grpc_host not in {"127.0.0.1", "localhost", "::1"} and not tls_configured:
        raise ValueError("remote Collector gRPC requires mTLS configuration")
    server = grpc.aio.server(
        options=(
            ("grpc.max_receive_message_length", 20 * 1024 * 1024),
            ("grpc.max_send_message_length", 20 * 1024 * 1024),
        ),
        maximum_concurrent_rpcs=32,
    )
    observer: DeploymentObserver | None = None
    repository_endpoint = os.environ.get("WIND_HUB_CONFIGURATION_ENDPOINT", "")
    if repository_endpoint:
        selection = select_local_service(config_path)
        observer = DeploymentObserver(
            deployment_id=selection.deployment_id,
            service_id=selection.service_id,
            endpoint=repository_endpoint,
            token_env="WIND_HUB_CONFIGURATION_TOKEN",
            nats_servers=list(messaging["servers"]),
        )
    task_control = CollectorTaskControl(configuration, lifecycle, intents, read_yaml)
    register_collector_rpc(
        server,
        common=CollectorConfigRpc(
            configuration, lifecycle, acquisition, telemetry, publisher, observer
        ),
        collector=CollectorDeviceRpc(acquisition, configuration, task_control, publisher),
    )
    endpoint = f"[{grpc_host}]:{grpc_port}" if ":" in grpc_host else f"{grpc_host}:{grpc_port}"
    if tls_configured:
        assert certificate_path is not None
        assert key_path is not None
        assert ca_path is not None
        credentials = grpc.ssl_server_credentials(
            [(Path(key_path).read_bytes(), Path(certificate_path).read_bytes())],
            root_certificates=Path(ca_path).read_bytes(),
            require_client_auth=True,
        )
        port = server.add_secure_port(endpoint, credentials)
    else:
        port = server.add_insecure_port(endpoint)
    if port == 0:
        raise RuntimeError(f"cannot bind Collector gRPC endpoint {endpoint}")
    ads_config = service.get("ads")
    ads_local = ADSLocalConfig(**ads_config) if ads_config else None
    workers = service.get("runtime", {}).get("thread_pool", {}).get("max_workers", 32)
    return CollectorApp(
        lifecycle,
        configuration,
        task_control,
        server,
        f"{grpc_host}:{port}",
        ads_local,
        workers,
        metrics_emitter,
        CollectorResourceSampler(telemetry) if metrics_emitter is not None else None,
        observer=observer,
    )
