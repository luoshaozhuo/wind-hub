"""Secure gRPC transport owned by the Commander process."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import grpc

from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.grpc.server import register_services
from commander.infrastructure.grpc.snapshots import ConfigSnapshots
from core.infrastructure.config import read_yaml


def _section(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name}: expected a string-keyed mapping")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: required nonempty string")
    return value


def _security_config(
    service_config: Path,
) -> tuple[str, int, int, grpc.ServerCredentials, dict[str, tuple[str, frozenset[str]]]]:
    raw = read_yaml(service_config)
    grpc_settings = _section(raw.get("grpc"), "grpc")
    security = _section(raw.get("security"), "security")
    host = _text(grpc_settings.get("listen_host"), "grpc.listen_host")
    port = grpc_settings.get("listen_port")
    maximum = grpc_settings.get("max_active_rpcs")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("grpc.listen_port must be in 1..65535")
    if type(maximum) is not int or maximum < 1:
        raise ValueError("grpc.max_active_rpcs must be positive")

    def certificate_bytes(field: str) -> bytes:
        value = Path(_text(security.get(field), f"security.{field}"))
        if not value.is_absolute():
            value = service_config.resolve().parent / value
        return value.read_bytes()

    ca = certificate_bytes("ca_cert")
    cert = certificate_bytes("server_cert")
    key = certificate_bytes("server_key")
    callers = security.get("callers")
    if not isinstance(callers, list) or not callers:
        raise ValueError("security.callers must be nonempty")
    permissions: dict[str, tuple[str, frozenset[str]]] = {}
    callers_seen: set[str] = set()
    for caller in callers:
        item = _section(caller, "security.callers entry")
        identity = _text(item.get("certificate_identity"), "certificate_identity")
        caller_id = _text(item.get("caller_id"), "caller_id")
        if caller_id in callers_seen:
            raise ValueError(f"duplicate caller_id: {caller_id}")
        callers_seen.add(caller_id)
        if identity in permissions:
            raise ValueError(f"duplicate client certificate identity: {identity}")
        granted = item.get("permissions")
        if (
            not isinstance(granted, list)
            or not granted
            or any(permission not in ("query", "reload", "control", "recover") for permission in granted)
        ):
            raise ValueError("invalid caller permissions")
        permissions[identity] = (caller_id, frozenset(granted))
    credentials = grpc.ssl_server_credentials(
        [(key, cert)], root_certificates=ca, require_client_auth=True
    )
    return host, port, maximum, credentials, permissions


async def start_grpc(service: CommanderService, service_config: Path) -> grpc.aio.Server:
    """Bind both Proto services. A plaintext endpoint is never opened."""
    host, port, maximum, credentials, permissions = _security_config(service_config)
    server = grpc.aio.server(maximum_concurrent_rpcs=maximum)
    snapshots = ConfigSnapshots(service, service_config)
    register_services(server, service, permissions, snapshots)
    bound = server.add_secure_port(f"{host}:{port}", credentials)
    if not bound:
        raise RuntimeError(f"unable to bind Commander gRPC listener at {host}:{port}")
    await server.start()
    return server


async def run_grpc(service: CommanderService, service_config: Path) -> None:
    """Start the application, serve gRPC and stop safely on cancellation."""
    await service.start()
    server: grpc.aio.Server | None = None
    try:
        server = await start_grpc(service, service_config)
        await server.wait_for_termination()
    finally:
        if server is not None:
            await server.stop(grace=5)
        operation = asyncio.create_task(service.stop())
        try:
            blocked = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Keep ownership until the native I/O shutdown decision is known.
            blocked = await operation
            if blocked:
                raise RuntimeError(
                    f"Commander has unsettled resources: {sorted(blocked)}"
                ) from None
            raise
        if blocked:
            raise RuntimeError(f"Commander has unsettled resources: {sorted(blocked)}")
