"""Process-local Sink Service bootstrap and infrastructure composition."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import grpc
import yaml

from sink_service.application.config import read_snapshot
from sink_service.application.config_manager import ConfigManager
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_registry import DeliveryPort, DeliveryRegistry
from sink_service.application.observability import Observability
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot, validate_service
from sink_service.infrastructure.config_repository import GenerationStore
from sink_service.infrastructure.config_sync import ConfigReader
from sink_service.infrastructure.grpc_v1 import build_v1_server
from sink_service.infrastructure.writer_lease import WriterLease

DeliveryFactory = Callable[[DeliveryDefinition, ValidatedSnapshot], DeliveryPort]


def _safe_managed_path(relative_path: str) -> bool:
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in ("", ".", "..") for part in relative_path.split("/"))
        or PurePosixPath(relative_path).as_posix() != relative_path
    ):
        return False
    return relative_path.startswith("configs/services/") and relative_path.endswith(".yaml")


def service_configuration(snapshot: ValidatedSnapshot, service_file: str) -> dict[str, Any]:
    """Select process-local Service configuration from a complete shared snapshot."""
    for item in snapshot.files:
        if item.path != service_file:
            continue
        value = yaml.safe_load(item.content)
        if not isinstance(value, dict):
            raise ConfigurationError("invalid Service configuration")
        validate_service(value)
        return value
    raise ConfigurationError(f"missing Service configuration: {service_file}")


def _credentials(security: dict[str, Any]) -> grpc.ServerCredentials:
    key = Path(security["server_key"]).read_bytes()
    certificate = Path(security["server_cert"]).read_bytes()
    client_ca = Path(security["ca_cert"]).read_bytes()
    return grpc.ssl_server_credentials(
        [(key, certificate)],
        root_certificates=client_ca,
        require_client_auth=True,
    )


async def serve_control_plane(
    config_root: Path,
    service_file: str,
    port_factory: DeliveryFactory,
    stop: asyncio.Event,
) -> None:
    """Run the control plane; data-plane implementation is injected by assembly."""
    if not _safe_managed_path(service_file):
        raise ConfigurationError("invalid Service relative path")
    store = GenerationStore(config_root)
    if (config_root / "CURRENT").exists():
        initial = store.load(service_file)
    else:
        initial = read_snapshot(config_root, config_root / service_file)
    service = service_configuration(initial, service_file)
    if Path(service["config_root"]).resolve() != config_root.resolve():
        raise ConfigurationError("Service config_root differs from runtime path")
    writer = service["config_writer"]
    role = service["process_role"]
    registry = DeliveryRegistry(role, initial)
    telemetry = Observability(
        service['runtime']['max_event_records'],
        window_ms=service['metrics']['metrics_window_ms'],
    )

    async def reconcile(snapshot: ValidatedSnapshot) -> tuple[str, ...]:
        return await registry.apply_configuration(snapshot, port_factory)

    manager = ConfigManager(store, initial, writer=writer, reconcile=reconcile)

    async def apply_reader_snapshot(snapshot: ValidatedSnapshot) -> None:
        if service_configuration(snapshot, service_file) != service:
            raise ConfigurationError("RESTART_REQUIRED: Reader Service configuration changed")
        await reconcile(snapshot)
        failed = [
            item.delivery_id
            for item in registry.list()
            if item.desired_state == "START" and item.actual_state != "RUNNING"
        ]
        if failed:
            raise ConfigurationError(f"Reader Deliveries not converged: {failed}")
        await manager.mark_active(snapshot)

    lease = WriterLease(config_root) if writer else None
    server: grpc.aio.Server | None = None
    poll_task: asyncio.Task[None] | None = None
    recovery_task: asyncio.Task[None] | None = None
    metrics_task: asyncio.Task[None] | None = None
    try:
        if lease is not None:
            lease.acquire()
            if not (config_root / "CURRENT").exists():
                # Publish the original validated filesystem snapshot before
                # the Reader starts polling.
                store.commit(initial, "", old=None)
        # Keep failed Deliveries visible while the Watchdog retries START intent.
        await reconcile(initial)
        grpc_config = service["grpc"]
        server = build_v1_server(
            config=manager,
            registry=registry,
            service_id=service["service_id"],
            role=role,
            credentials=_credentials(service["security"]),
            address=f"{grpc_config['listen_host']}:{grpc_config['listen_port']}",
            max_active_rpcs=grpc_config["max_active_rpcs"],
            max_request_bytes=grpc_config["max_request_bytes"],
            callers=service["security"]["callers"],
            observability=telemetry,
        )
        await server.start()

        watchdog_seconds = service["runtime"].get("watchdog_interval_ms", 5000) / 1000

        async def recover_running() -> None:
            while not stop.is_set():
                await registry.reconcile_all()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=watchdog_seconds)
                except TimeoutError:
                    continue

        recovery_task = asyncio.create_task(recover_running())

        async def collect_metrics() -> None:
            previous_states: dict[str, tuple[str, str]] = {}
            interval = service['metrics']['metrics_compute_interval_ms'] / 1000
            loop = asyncio.get_running_loop()
            next_tick = loop.time() + interval
            while not stop.is_set():
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=max(0.0, next_tick - loop.time())
                    )
                except TimeoutError:
                    pass
                if stop.is_set():
                    return
                current = loop.time()
                next_tick += max(1, int((current - next_tick) / interval) + 1) * interval
                views = registry.list()
                counters = registry.counters()
                telemetry.sample(
                    counters,
                    latency_observations=registry.latency_observations(),
                    latency_overflow_at=registry.latency_overflow_at(),
                )
                states = {
                    view.delivery_id: (view.actual_state, view.last_error)
                    for view in views
                }
                for identifier, state in states.items():
                    if previous_states.get(identifier) != state:
                        telemetry.event(
                            'ERROR' if state[0] == 'FAILED' else 'INFO',
                            identifier, 'DELIVERY_STATE', f'{state[0]}: {state[1]}',
                        )
                previous_states = states
        metrics_task = asyncio.create_task(collect_metrics())
        if not writer:
            sync = service["config_sync"]
            reader = ConfigReader(
                config_root,
                service_file,
                apply_reader_snapshot,
                poll_interval_ms=sync["poll_interval_ms"],
            )
            poll_task = asyncio.create_task(reader.run(stop))
        await stop.wait()
    finally:
        stop.set()
        if poll_task is not None:
            await poll_task
        if recovery_task is not None:
            await recovery_task
        if metrics_task is not None:
            await metrics_task
        if server is not None:
            await server.stop(grace=float(service["shutdown"]["grace_seconds"]))
        # The Writer lease must be released even if an Adapter shutdown fails.
        try:
            await registry.shutdown()
        finally:
            if lease is not None:
                lease.release()
