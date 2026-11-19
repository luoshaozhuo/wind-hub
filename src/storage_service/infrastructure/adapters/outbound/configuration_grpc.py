"""Read-only Configuration Repository client and immutable Storage bundle loader.

The current repository server still uses mTLS. This adapter intentionally has
no certificate fallback; connection failure is a deployment gate until the
repository implements the new trusted-network/token contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import grpc

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigHistoryUnavailable,
    ImmutableConfigArchive,
    RepositoryDocument,
)


@dataclass(frozen=True, slots=True)
class StorageBundle:
    """Pinned, verified service configuration and exact dependent semantic versions."""

    configuration: StorageConfig
    revision_id: int
    point_table_versions: dict[str, str]
    business_points_pin: tuple[str, str] | None


def _document(item: pb.ConfigYamlDocument) -> RepositoryDocument:
    if not item.version_id or not item.config_id.config_type or not item.config_id.config_id:
        raise ConfigHistoryUnavailable("configuration response lacks stable identity")
    result = RepositoryDocument(
        config_type=item.config_id.config_type,
        config_id=item.config_id.config_id,
        version_id=item.version_id,
        revision_id=item.revision_id,
        yaml_bytes=bytes(item.yaml_bytes),
        yaml_sha256=item.yaml_sha256,
    )
    if hashlib.sha256(result.yaml_bytes).hexdigest() != result.yaml_sha256:
        raise ConfigHistoryUnavailable("configuration repository YAML digest mismatch")
    return result


class ConfigurationGrpcClient:
    """Request exact versions and revision-pinned bundle; never use a latest-version alias."""

    def __init__(
        self, *, endpoint: str, token_env: str, archive: ImmutableConfigArchive,
        timeout_s: float = 10.0,
    ) -> None:
        if not endpoint or not token_env or timeout_s <= 0:
            raise ValueError("Configuration client endpoint/token/deadline required")
        self._endpoint = endpoint
        self._token_env = token_env
        self._archive = archive
        self._timeout = timeout_s
        self._channel: grpc.aio.Channel | None = None
        self._stub: rpc.ConfigurationRepositoryServiceStub | None = None

    def _metadata(self) -> Sequence[tuple[str, str]]:
        token = os.getenv(self._token_env)
        if not token:
            raise ConfigHistoryUnavailable("Configuration service token missing")
        return (("authorization", f"Bearer {token}"),)

    async def open(self) -> None:
        if self._channel is not None:
            raise RuntimeError("Configuration channel already open")
        channel = grpc.aio.insecure_channel(self._endpoint)
        self._channel = channel
        self._stub = rpc.ConfigurationRepositoryServiceStub(channel)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._stub = None

    def _client(self) -> rpc.ConfigurationRepositoryServiceStub:
        if self._stub is None:
            raise RuntimeError("Configuration client not connected")
        return self._stub

    async def fetch_by_version(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> RepositoryDocument:
        response = await self._client().GetConfig(
            pb.GetConfigRequest(
                config_id=pb.ConfigId(config_type=config_type, config_id=config_id),
                version_id=version_id,
            ),
            timeout=self._timeout,
            metadata=self._metadata(),
        )
        document = _document(response.document)
        if (document.config_type, document.config_id, document.version_id) != (
            config_type, config_id, version_id
        ):
            raise ConfigHistoryUnavailable("configuration repository returned wrong version")
        await asyncio.to_thread(self._archive.persist, document)
        return document

    async def fetch_service_bundle(self, *, service_id: str) -> StorageBundle:
        """Resolve one authority revision then validate every returned YAML byte."""
        listing = await self._client().ListConfigs(
            pb.ListConfigsRequest(
                config_type="service", config_id_prefix=service_id, page_size=100,
            ),
            timeout=self._timeout,
            metadata=self._metadata(),
        )
        if listing.next_page_token:
            raise ConfigHistoryUnavailable("service listing was truncated")
        roots = [
            entry for entry in listing.items
            if entry.config_id.config_type == "service" and entry.config_id.config_id == service_id
        ]
        if len(roots) != 1 or listing.snapshot_revision_id <= 0:
            raise ConfigHistoryUnavailable("service config absent or revision unspecified")
        revision = listing.snapshot_revision_id
        response = await self._client().GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[pb.ConfigId(config_type="service", config_id=service_id)],
                revision_id=revision,
                include_dependencies=True,
            ),
            timeout=self._timeout,
            metadata=self._metadata(),
        )
        if response.revision_id != revision:
            raise ConfigHistoryUnavailable("Configuration bundle revision mismatch")
        digest = hashlib.sha256()
        objects: dict[tuple[str, str], RepositoryDocument] = {}
        for item in response.files:
            digest.update(item.version_id.encode("utf-8"))
            digest.update(item.yaml_sha256.encode("utf-8"))
            document = _document(item)
            key = (document.config_type, document.config_id)
            if key in objects:
                raise ConfigHistoryUnavailable("duplicate config identity in one closure")
            objects[key] = document
        if digest.hexdigest() != response.manifest_sha256:
            raise ConfigHistoryUnavailable("Configuration bundle manifest SHA256 mismatch")
        if sum(len(d.yaml_bytes) for d in objects.values()) != response.total_bytes:
            raise ConfigHistoryUnavailable("Configuration bundle byte count mismatch")
        service = objects.get(("service", service_id))
        if service is None or service.version_id != roots[0].recommended_version_id:
            raise ConfigHistoryUnavailable("service version differs from pinned revision")
        configuration = load_storage_config(service.yaml_bytes)
        if configuration.service_id != service_id:
            raise ConfigHistoryUnavailable("service config identity mismatch")
        # Persist complete closure before acknowledging the new config generation.
        for document in objects.values():
            await asyncio.to_thread(self._archive.persist, document)
        points = {
            key[1]: document.version_id
            for key, document in objects.items() if key[0] == "point_table"
        }
        business = [key for key in objects if key[0] == "business_points"]
        if len(business) > 1:
            raise ConfigHistoryUnavailable("ambiguous business point catalog")
        pin = (
            (business[0][1], objects[business[0]].version_id)
            if business else None
        )
        return StorageBundle(
            configuration=configuration,
            revision_id=revision,
            point_table_versions=points,
            business_points_pin=pin,
        )
