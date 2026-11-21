"""Verified per-Service Configuration Repository query and revision polling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import grpc
import nats

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from core.configuration.deployment import closure_sha256
from core.configuration.publication_identity import resolve_publication_identity
from core.infrastructure.config.generic_yaml import parse_yaml

_LOG = logging.getLogger(__name__)
_MAX_FILES = 1024
_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SelectedBundle:
    """One authenticated, immutable target closure from the Repository."""

    target_revision: str
    repository_revision: int
    files: dict[str, bytes]


def verify_selected_bundle(
    reply: pb.GetServiceConfigBundleResponse, deployment_id: str, service_id: str
) -> SelectedBundle:
    """Reject incomplete, mismatched, or over-limit Repository responses."""
    if not 0 < len(reply.files) <= _MAX_FILES or reply.repository_revision_id < 1:
        raise ValueError("invalid Service configuration bundle size or repository revision")
    total = 0
    manifest = hashlib.sha256()
    files: dict[str, bytes] = {}
    identity: list[tuple[str, str, str, str]] = []
    for item in reply.files:
        path = item.relative_path
        raw = bytes(item.yaml_bytes)
        total += len(raw)
        if total > _MAX_BYTES or path in files:
            raise ValueError("oversized or duplicate Service configuration file")
        for value in (path, item.version_id, item.yaml_sha256):
            encoded = value.encode("utf-8")
            manifest.update(len(encoded).to_bytes(4, "big"))
            manifest.update(encoded)
        if hashlib.sha256(raw).hexdigest() != item.yaml_sha256:
            raise ValueError("configuration file content hash mismatch")
        payload = parse_yaml(raw.decode("utf-8", errors="strict"))
        if not isinstance(payload, dict) or (
            payload.get("schema_version") != item.schema_version
            or payload.get("file_version") != item.file_version
        ):
            raise ValueError("configuration file schema or version mismatch")
        publication = resolve_publication_identity(path, payload)
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != item.content_sha256:
            raise ValueError("configuration semantic digest mismatch")
        identity.append((
            publication.config_type, publication.config_id,
            publication.file_version, item.content_sha256,
        ))
        files[path] = raw
    if total != reply.total_bytes or manifest.hexdigest() != reply.manifest_sha256:
        raise ValueError("Service configuration manifest hash mismatch")
    expected = closure_sha256(deployment_id, service_id, identity)
    if reply.target_revision != expected:
        raise ValueError("Service target revision digest mismatch")
    selected = [
        path for path, content in files.items()
        if path.startswith("services/")
        and resolve_publication_identity(path, parse_yaml(content.decode("utf-8"))).config_id
        == service_id
    ]
    if len(selected) != 1:
        raise ValueError("Service configuration bundle has no unique Service")
    return SelectedBundle(expected, reply.repository_revision_id, files)


class DeploymentObserver:
    """Poll and subscribe independently; never mark a failed Apply active."""

    def __init__(
        self,
        *,
        deployment_id: str,
        service_id: str,
        endpoint: str,
        token_env: str,
        nats_servers: list[str],
        interval_seconds: int = 30,
    ) -> None:
        if not all((deployment_id, service_id, endpoint, token_env)):
            raise ValueError("Deployment observer needs bootstrap identity and endpoint")
        if interval_seconds < 5 or interval_seconds > 3600:
            raise ValueError("invalid configuration reconcile interval")
        self.deployment_id = deployment_id
        self.service_id = service_id
        self.endpoint = endpoint
        self.token_env = token_env
        self.nats_servers = nats_servers
        self.interval_seconds = interval_seconds
        self.target_revision = ""
        self.active_revision = ""
        self.last_error = ""
        self.repository_revision = 0
        self.restart_required = False

    async def fetch(self) -> SelectedBundle:
        token = os.environ.get(self.token_env, "")
        if len(token) < 32 or token.strip() != token:
            raise ValueError("Configuration query credentials unavailable")
        metadata = (("authorization", f"Bearer {token}"),)
        async with grpc.aio.insecure_channel(
            self.endpoint, options=[("grpc.max_receive_message_length", _MAX_BYTES + 1048576)]
        ) as channel:
            reply = await rpc.ConfigurationRepositoryServiceStub(channel).GetServiceConfigBundle(
                pb.GetServiceConfigBundleRequest(
                    deployment_id=self.deployment_id,
                    service_id=self.service_id,
                    revision_id=0,
                ),
                timeout=20,
                metadata=metadata,
            )
        return verify_selected_bundle(reply, self.deployment_id, self.service_id)

    async def reconcile(
        self, apply: Callable[[SelectedBundle], Awaitable[bool]]
    ) -> None:
        """Apply only a different target and preserve the last active identity."""
        try:
            bundle = await self.fetch()
            self.repository_revision = bundle.repository_revision
            if (
                bundle.target_revision == self.target_revision
                and self.active_revision == self.target_revision
                and not self.last_error
            ):
                return
            self.target_revision = bundle.target_revision
            complete = await apply(bundle)
            if complete:
                self.active_revision = bundle.target_revision
                self.restart_required = False
            else:
                self.restart_required = True
            self.last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc) or type(exc).__name__
            _LOG.warning("Service configuration convergence deferred: %s", self.last_error)

    async def run(
        self, stop: asyncio.Event, apply: Callable[[SelectedBundle], Awaitable[bool]]
    ) -> None:
        """Treat NATS notices as wakeups; authoritative polling also runs offline."""
        wake = asyncio.Event()
        wake.set()
        client = None
        try:
            while not stop.is_set():
                if client is None and self.nats_servers:
                    try:
                        async def on_notice(_message: object) -> None:
                            wake.set()

                        async def on_reconnect() -> None:
                            wake.set()

                        client = await nats.connect(
                            servers=self.nats_servers,
                            connect_timeout=2,
                            max_reconnect_attempts=-1,
                            reconnected_cb=on_reconnect,
                        )
                        await client.subscribe("wh.config.changed", cb=on_notice)
                    except Exception:
                        _LOG.warning("configuration broadcast unavailable; using polling")
                        if client is not None:
                            await client.close()
                        client = None
                await self.reconcile(apply)
                waking = asyncio.create_task(wake.wait())
                stopping = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait(
                        (waking, stopping),
                        timeout=self.interval_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    wake.clear()
                finally:
                    for task in (waking, stopping):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(waking, stopping, return_exceptions=True)
        finally:
            if client is not None:
                await client.close()
