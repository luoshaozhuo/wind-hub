"""Secure gRPC transport owned by the Commander process."""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import grpc

from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.adapters.inbound.grpc.server import register_services
from commander.infrastructure.adapters.inbound.grpc.snapshots import ConfigSnapshots
from commander.infrastructure.adapters.outbound.recovery_authority import IsolationAuthorityClient
from commander.infrastructure.config.service_settings import validate_transport_settings
from core.infrastructure.config import read_yaml
from core.infrastructure.configuration_repository import DeploymentObserver, SelectedBundle


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
) -> tuple[str, int, int, dict[str, tuple[str, frozenset[str]]]]:
    """Resolve nonempty unique caller tokens and enforce loopback-only transport."""
    raw = read_yaml(service_config)
    validate_transport_settings(raw)
    grpc_settings = _section(raw.get("grpc"), "grpc")
    security = _section(raw.get("security"), "security")
    host = _text(grpc_settings.get("listen_host"), "grpc.listen_host")
    port = grpc_settings["listen_port"]
    maximum = grpc_settings["max_active_rpcs"]
    callers = security["callers"]
    permissions: dict[str, tuple[str, frozenset[str]]] = {}
    for caller in callers:
        item = _section(caller, "security.callers entry")
        reference = _text(item.get("token_env"), "caller.token_env")
        token = os.environ.get(reference, "")
        if len(token) < 32 or not token.isascii() or any(not 33 <= ord(ch) <= 126 for ch in token):
            raise ValueError(f"caller {item['caller_id']}: invalid or missing token")
        if any(hmac.compare_digest(token, old) for old in permissions):
            raise ValueError("duplicate security caller token")
        permissions[token] = (item["caller_id"], frozenset(item["permissions"]))
    return host, port, maximum, permissions


async def start_grpc(service: CommanderService, service_config: Path) -> grpc.aio.Server:
    """Bind both Proto services on authenticated loopback-only gRPC."""
    host, port, maximum, permissions = _security_config(service_config)
    server = grpc.aio.server(maximum_concurrent_rpcs=maximum)
    snapshots = ConfigSnapshots(service, service_config)
    # No recovery configuration means the RPC remains fail-closed.
    config = read_yaml(service_config)
    security = _section(config.get("security"), "security")
    recovery = security.get("recovery")
    authority: IsolationAuthorityClient | None = None
    if recovery is not None:
        item = _section(recovery, "security.recovery")

        def relative_path(name: str) -> Path:
            path = Path(_text(item.get(name), f"security.recovery.{name}"))
            return path if path.is_absolute() else service_config.resolve().parent / path

        timeout_value = item.get("timeout_seconds", 5.0)
        if type(timeout_value) not in (int, float) or timeout_value <= 0:
            raise ValueError("invalid recovery authority timeout")
        authority = IsolationAuthorityClient(
            url=_text(item.get("url"), "security.recovery.url"),
            ca_cert=relative_path("ca_cert"),
            client_cert=relative_path("client_cert"),
            client_key=relative_path("client_key"),
            instance_id=_text(item.get("instance_id"), "security.recovery.instance_id"),
            timeout=float(timeout_value),
        )
    register_services(server, service, permissions, snapshots, authority)
    bound = server.add_insecure_port(f"{host}:{port}")
    if not bound:
        raise RuntimeError(f"unable to bind Commander gRPC listener at {host}:{port}")
    await server.start()
    return server


async def run_grpc(
    service: CommanderService,
    service_config: Path,
    *,
    observer: DeploymentObserver | None = None,
    apply_selected: Callable[[SelectedBundle], Awaitable[bool]] | None = None,
    on_started: Callable[[], None] | None = None,
) -> None:
    """Serve gRPC and reconcile Deployment while preserving I/O ownership."""
    await service.start()
    server: grpc.aio.Server | None = None
    stop = asyncio.Event()
    task: asyncio.Task[None] | None = None
    try:
        server = await start_grpc(service, service_config)
        if on_started is not None:
            on_started()
        if observer is not None and apply_selected is not None:
            task = asyncio.create_task(observer.run(stop, apply_selected))
        await server.wait_for_termination()
    finally:
        stop.set()
        if task is not None:
            await task
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
