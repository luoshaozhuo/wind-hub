"""Independent Configuration revision reconciliation for Sink Main.

NATS events only wake the reconciler. Repository revision and a verified
complete closure are authoritative; loss of a notification is harmless.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import grpc
import nats

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from core.infrastructure.config.generic_yaml import parse_yaml
from core.infrastructure.configuration_repository import verify_selected_bundle
from sink_service.application.config import template_path
from sink_service.application.config_manager import ConfigManager
from sink_service.application.config_sources import config_sources
from sink_service.domain.config import ConfigurationError, SnapshotFile
from sink_service.infrastructure.config_io import validate_snapshot
from sink_service.infrastructure.secrets import resolve_secret

_LOG = logging.getLogger(__name__)
_SUBJECT = "wh.config.changed"
_MAX_BUNDLE_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENTS = 1000


def _payloads(
    bundle: pb.GetServiceConfigBundleResponse,
    deployment_id: str,
    service_id: str,
) -> dict[str, bytes]:
    """Verify the published complete immutable closure before any application."""
    selected = verify_selected_bundle(bundle, deployment_id, service_id)
    if not 0 < len(selected.files) <= _MAX_DOCUMENTS:
        raise ConfigurationError("Sink selected bundle exceeds document limit")
    if sum(map(len, selected.files.values())) > _MAX_BUNDLE_BYTES:
        raise ConfigurationError("Sink selected bundle exceeds byte limit")
    return {"configs/" + name: value for name, value in selected.files.items()}


def _local_snapshot(files: Mapping[str, bytes]) -> tuple[SnapshotFile, ...]:
    """Extract a strict Sink closure from the verified repository response."""
    candidates = [path for path in files if path.startswith("configs/services/")]
    if len(candidates) != 1:
        raise ConfigurationError("selected Service bundle requires exactly one Service")
    service_path = candidates[0]
    service = parse_yaml(files[service_path].decode("utf-8"))
    sources, template_root = config_sources(service, files)
    required: set[str] = {service_path, *sources.values()}
    for family, pathname in sources.items():
        content = files.get(pathname)
        if content is None:
            raise ConfigurationError("Configuration bundle misses Sink Instance")
        document = parse_yaml(content.decode("utf-8"))
        objects = document.get(family)
        if not isinstance(objects, dict) or not objects:
            raise ConfigurationError("Configuration bundle has invalid Sink Instances")
        for entry in objects.values():
            if not isinstance(entry, dict):
                raise ConfigurationError("Configuration bundle has invalid Sink Instance")
            template_id = entry.get("template_id")
            template_version = entry.get("template_version")
            if not isinstance(template_id, str) or not isinstance(template_version, str):
                raise ConfigurationError("Configuration bundle has invalid template selector")
            required.add(template_path(template_root, family, template_id, template_version))
    if not required.issubset(files):
        raise ConfigurationError("Configuration bundle misses referenced Sink templates")
    return tuple(SnapshotFile(name, files[name]) for name in sorted(required))


class ConfigurationSubscriber:
    """Converge independently after reconnect or lost broadcast."""

    def __init__(self, manager: ConfigManager, service: dict[str, Any]) -> None:
        sync = service["config_sync"]
        self._endpoint: str = sync["repository_endpoint"]
        host = self._endpoint.rsplit(":", 1)[0].strip("[]")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ConfigurationError("Bearer Repository endpoint must be loopback")
        self._token_ref: str = sync["repository_token_ref"]
        self._servers: list[str] = list(service["nats"]["servers"])
        self._interval: int = sync.get("poll_interval_seconds", 30)
        self._manager = manager
        self._revision = 0
        self._target_revision = ""
        self._deployment_id = service["deployment_id"]
        self._service_id = service["service_id"]

    async def _reconcile(self) -> None:
        token = resolve_secret(self._token_ref)
        if not token or len(token) < 32:
            raise ConfigurationError("Configuration query token is unavailable")
        metadata = (("authorization", f"Bearer {token}"),)
        async with grpc.aio.insecure_channel(
            self._endpoint,
            options=[("grpc.max_receive_message_length", _MAX_BUNDLE_BYTES + 1048576)],
        ) as channel:
            stub = rpc.ConfigurationRepositoryServiceStub(channel)
            bundle = await stub.GetServiceConfigBundle(
                pb.GetServiceConfigBundleRequest(
                    deployment_id=self._deployment_id,
                    service_id=self._service_id,
                    revision_id=0,
                ),
                timeout=20,
                metadata=metadata,
            )
        if bundle.repository_revision_id < 1 or len(bundle.target_revision) != 64:
            raise ConfigurationError("Configuration Service bundle has invalid revision")
        if (
            self._target_revision == bundle.target_revision
            and self._manager.active_revision == bundle.target_revision
        ):
            self._revision = bundle.repository_revision_id
            return
        local = _local_snapshot(_payloads(bundle, self._deployment_id, self._service_id))
        verified = validate_snapshot(local)
        if verified.revision == self._manager.disk.revision:
            converged = (
                self._manager.disk.revision == self._manager.runtime.revision
                or await self._manager.reconcile_committed()
            )
            self._manager.report_selected(bundle.target_revision, active=converged)
            self._revision = bundle.repository_revision_id
            self._target_revision = bundle.target_revision
            return
        wire = [
            (
                item.path,
                item.content,
                len(item.content),
                hashlib.sha256(item.content).hexdigest(),
            )
            for item in local
        ]
        candidate_id, _ = await self._manager.push(wire, self._manager.disk.revision)
        result = await self._manager.apply(candidate_id, self._manager.disk.revision)
        self._revision = bundle.repository_revision_id
        self._target_revision = bundle.target_revision
        self._manager.report_selected(
            bundle.target_revision,
            active=(
                not result.restart_required
                and not result.pending
                and not result.error
                and self._manager.disk.revision == self._manager.runtime.revision
            ),
        )
        if result.restart_required or result.pending or result.error:
            _LOG.warning(
                "Sink configuration committed but application is pending restart/convergence"
            )
        else:
            _LOG.info("Sink configuration converged at Repository revision %d", self._revision)

    async def run(self, stop: asyncio.Event) -> None:
        """Never let NATS unavailability prevent authoritative periodic polling."""
        wake = asyncio.Event()
        wake.set()
        client: Any = None
        try:
            while not stop.is_set():
                if client is None:
                    try:

                        async def on_reconnect() -> None:
                            wake.set()

                        async def on_notice(_message: Any) -> None:
                            wake.set()

                        client = await nats.connect(
                            servers=self._servers,
                            connect_timeout=2,
                            max_reconnect_attempts=-1,
                            reconnected_cb=on_reconnect,
                        )
                        await client.subscribe(_SUBJECT, cb=on_notice)
                        wake.set()
                    except Exception:
                        _LOG.warning(
                            "Sink revision broadcast unavailable; polling",
                            exc_info=True,
                        )
                        if client is not None:
                            await client.close()
                        client = None
                try:
                    await self._reconcile()
                except Exception:
                    _LOG.warning("Sink Configuration reconciliation deferred", exc_info=True)
                wake_task = asyncio.create_task(wake.wait())
                stop_task = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait(
                        (wake_task, stop_task),
                        timeout=self._interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    wake.clear()
                finally:
                    for task in (wake_task, stop_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(wake_task, stop_task, return_exceptions=True)
        finally:
            if client is not None:
                await client.close()
