"""Process-local v1 control plane with externally supplied data-plane factory."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import grpc
import yaml

from sink_service.application.config import read_snapshot
from sink_service.application.config_manager import ConfigManager
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_registry import DeliveryPort, DeliveryRegistry
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot, validate_service
from sink_service.infrastructure.config_repository import GenerationStore
from sink_service.infrastructure.config_sync import ConfigReader
from sink_service.infrastructure.grpc_v1 import build_v1_server
from sink_service.infrastructure.writer_lease import WriterLease

DeliveryFactory = Callable[[DeliveryDefinition, ValidatedSnapshot], DeliveryPort]


def service_configuration(
    snapshot: ValidatedSnapshot, service_file: str
) -> dict[str, Any]:
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
    if not service_file.startswith("services/") or not service_file.endswith(".yaml"):
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

    async def reconcile(snapshot: ValidatedSnapshot) -> tuple[str, ...]:
        return await registry.apply_configuration(snapshot, port_factory)

    manager = ConfigManager(store, initial, writer=writer, reconcile=reconcile)

    async def apply_reader_snapshot(snapshot: ValidatedSnapshot) -> None:
        if service_configuration(snapshot, service_file) != service:
            raise ConfigurationError("RESTART_REQUIRED: Reader Service configuration changed")
        await reconcile(snapshot)
        await manager.mark_active(snapshot)
    lease = WriterLease(config_root) if writer else None
    if lease is not None:
        lease.acquire()
    server: grpc.aio.Server | None = None
    poll_task: asyncio.Task[None] | None = None
    try:
        # A failed initial reconciliation prevents false READY status.
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
        )
        await server.start()
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
        if server is not None:
            await server.stop(grace=5.0)
        # The Writer lease must be released even if an Adapter shutdown fails.
        try:
            await registry.shutdown()
        finally:
            if lease is not None:
                lease.release()
