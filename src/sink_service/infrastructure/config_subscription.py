"""Independent Configuration revision reconciliation for Sink Main.

NATS events only wake the reconciler. Repository revision and a verified
complete closure are authoritative; loss of a notification is harmless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

import grpc
import nats

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from core.infrastructure.config.generic_yaml import ConfigOperationError, parse_yaml
from sink_service.application.config import template_path
from sink_service.application.config_manager import ConfigManager
from sink_service.application.config_sources import config_sources
from sink_service.domain.config import ConfigurationError, SnapshotFile
from sink_service.infrastructure.config_io import validate_snapshot
from sink_service.infrastructure.secrets import resolve_secret

_LOG = logging.getLogger(__name__)
_SUBJECT = "wh.config.changed"
_ROOT = pb.ConfigId(config_type="service", config_id="sink_service")
_MAX_BUNDLE_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENTS = 1000


def _payloads(bundle: pb.GetConfigBundleResponse) -> dict[str, bytes]:
    """Authenticate all source files before selecting a local Sink closure."""
    if not 0 < len(bundle.files) <= _MAX_DOCUMENTS:
        raise ConfigurationError("invalid Configuration bundle file count")
    digest = hashlib.sha256()
    files: dict[str, bytes] = {}
    total = 0
    for item in bundle.files:
        relative = item.relative_path
        if (
            not relative
            or relative.startswith("/")
            or "\\" in relative
            or "\x00" in relative
            or any(part in ("", ".", "..") for part in relative.split("/"))
            or not relative.endswith(".yaml")
        ):
            raise ConfigurationError("invalid Configuration bundle relative path")
        for value in (relative, item.version_id, item.yaml_sha256):
            data = value.encode("utf-8")
            digest.update(len(data).to_bytes(4, "big"))
            digest.update(data)
        path = relative if relative.startswith("configs/") else "configs/" + relative
        if path in files or not item.version_id:
            raise ConfigurationError("duplicate or incomplete Configuration bundle file")
        data = bytes(item.yaml_bytes)
        total += len(data)
        if total > _MAX_BUNDLE_BYTES:
            raise ConfigurationError("Configuration bundle exceeds local capacity")
        if hashlib.sha256(data).hexdigest() != item.yaml_sha256:
            raise ConfigurationError("Configuration source YAML hash mismatch")
        try:
            document = parse_yaml(data.decode("utf-8", errors="strict"))
        except (ConfigOperationError, UnicodeError) as exc:
            raise ConfigurationError("invalid Configuration source YAML") from exc
        if (
            document.get("file_version") != item.file_version
            or document.get("schema_version") != item.schema_version
        ):
            raise ConfigurationError("Configuration bundle version/header mismatch")
        semantic = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(semantic).hexdigest() != item.content_sha256:
            raise ConfigurationError("Configuration bundle semantic hash mismatch")
        files[path] = data
    if (
        digest.hexdigest() != bundle.manifest_sha256
        or total != bundle.total_bytes
    ):
        raise ConfigurationError("Configuration bundle manifest mismatch")
    return files


def _local_snapshot(files: Mapping[str, bytes]) -> tuple[SnapshotFile, ...]:
    """Extract a strict Sink closure from the verified repository response."""
    service_path = "configs/services/sink_service.yaml"
    data = files.get(service_path)
    if data is None:
        raise ConfigurationError("Configuration bundle omits Sink Service")
    service = parse_yaml(data.decode("utf-8"))
    sources, template_root = config_sources(service)
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

    def __init__(
        self, manager: ConfigManager, service: dict[str, Any]
    ) -> None:
        sync = service["config_sync"]
        self._endpoint: str = sync["repository_endpoint"]
        self._token_ref: str = sync["repository_token_ref"]
        self._servers: list[str] = list(service["nats"]["servers"])
        self._interval: int = sync.get("poll_interval_seconds", 30)
        self._manager = manager
        self._revision = 0

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
            current = await stub.GetCurrentRevision(
                pb.GetCurrentRevisionRequest(), timeout=10, metadata=metadata
            )
            if current.revision_id < 1:
                raise ConfigurationError("Configuration has no committed revision")
            if current.revision_id == self._revision:
                return
            bundle = await stub.GetConfigBundle(
                pb.GetConfigBundleRequest(
                    roots=[_ROOT],
                    revision_id=current.revision_id,
                    include_dependencies=True,
                ),
                timeout=20,
                metadata=metadata,
            )
        if bundle.revision_id != current.revision_id:
            raise ConfigurationError("Configuration bundle revision mismatch")
        local = _local_snapshot(_payloads(bundle))
        verified = validate_snapshot(local)
        if verified.revision == self._manager.disk.revision:
            self._revision = current.revision_id
            return
        wire = [
            (item.path, item.content, len(item.content), hashlib.sha256(item.content).hexdigest())
            for item in local
        ]
        candidate_id, _ = await self._manager.push(
            wire, self._manager.disk.revision
        )
        result = await self._manager.apply(
            candidate_id, self._manager.disk.revision
        )
        self._revision = current.revision_id
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
                        _LOG.warning("Sink revision broadcast unavailable; polling", exc_info=True)
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
