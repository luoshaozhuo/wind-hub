"""Read-only bearer-authorized Configuration client and immutable bundle loader.

No certificate-based fallback, anonymous gRPC call, or latest-version alias is
allowed. Configuration publishes exact immutable versions for Storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

import grpc
import yaml

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from storage_service.application.config import (
    DeviceBinding,
    DeviceClosurePins,
    DeviceModel,
    FixedConfigSource,
    StorageConfig,
    load_storage_config,
)
from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ImmutableConfigArchive,
    RepositoryDocument,
)


@dataclass(frozen=True, slots=True)
class StorageBundle:
    """Pinned service, semantic and device-model/instance configuration closure."""

    configuration: StorageConfig
    revision_id: int
    point_table_versions: dict[str, tuple[str, str]]
    business_points_pin: tuple[str, str] | None
    device_bindings: tuple[DeviceBinding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        pins = self.point_table_versions
        if isinstance(pins, DeviceClosurePins):
            if self.device_bindings and self.device_bindings != pins.device_bindings:
                raise ConfigHistoryUnavailableError("Storage bundle device closures disagree")
            object.__setattr__(self, "device_bindings", pins.device_bindings)
            return
        object.__setattr__(
            self,
            "point_table_versions",
            DeviceClosurePins(pins, self.device_bindings),
        )


def _document(item: pb.ConfigYamlDocument) -> RepositoryDocument:
    if not item.version_id or not item.config_id.config_type or not item.config_id.config_id:
        raise ConfigHistoryUnavailableError("configuration response lacks stable identity")
    result = RepositoryDocument(
        config_type=item.config_id.config_type,
        config_id=item.config_id.config_id,
        version_id=item.version_id,
        revision_id=item.revision_id,
        yaml_bytes=bytes(item.yaml_bytes),
        yaml_sha256=item.yaml_sha256,
    )
    if hashlib.sha256(result.yaml_bytes).hexdigest() != result.yaml_sha256:
        raise ConfigHistoryUnavailableError("configuration repository YAML digest mismatch")
    return result


_ConfigKey = tuple[str, str, str]


def _yaml_object(document: RepositoryDocument) -> dict[str, object]:
    try:
        raw = yaml.safe_load(document.yaml_bytes)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ConfigHistoryUnavailableError("configuration YAML cannot be parsed") from exc
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ConfigHistoryUnavailableError("configuration document must be a string-keyed object")
    return raw


def _point_table_identity(key: _ConfigKey, raw: dict[str, object]) -> str:
    _config_type, config_id, file_version = key
    if (
        raw.get("schema_version") != 1
        or type(raw.get("schema_version")) is not int
        or raw.get("template_id") != config_id
        or raw.get("file_version") != file_version
    ):
        raise ConfigHistoryUnavailableError("PointTable document identity is inconsistent")
    point_table_id = f"point_table_{config_id}_v{file_version}"
    if len(point_table_id.encode("utf-8")) > 256:
        raise ConfigHistoryUnavailableError("PointTable identity exceeds wire limit")
    return point_table_id


def _parse_device_closure(
    objects: dict[_ConfigKey, RepositoryDocument],
    sources: Sequence[FixedConfigSource],
) -> tuple[dict[str, tuple[str, str]], tuple[DeviceBinding, ...]]:
    """Resolve exact Site/device/model/table references without replacing history."""
    required: dict[tuple[str, str], set[str]] = {}
    for source in sources:
        required.setdefault((source.config_type, source.config_id), set()).add(source.file_version)
    documents = {
        key: _yaml_object(document)
        for key, document in objects.items()
        if key[0] in {"point_table", "device_model", "device", "site"}
    }
    models: dict[tuple[str, str], DeviceModel] = {}
    model_version_ids: dict[str, DeviceModel] = {}
    model_point_keys: set[_ConfigKey] = set()
    for key, document in objects.items():
        config_type, config_id, file_version = key
        if config_type != "device_model":
            continue
        raw = documents[key]
        if (
            raw.get("schema_version") != 1
            or type(raw.get("schema_version")) is not int
            or raw.get("template_id") != config_id
            or raw.get("file_version") != file_version
        ):
            raise ConfigHistoryUnavailableError("DeviceModel document identity is inconsistent")
        device_type_id = raw.get("device_type_id")
        table = raw.get("point_table")
        if not isinstance(device_type_id, str) or not device_type_id or not isinstance(table, dict):
            raise ConfigHistoryUnavailableError("DeviceModel requires device_type_id/point_table")
        table_id = table.get("id")
        table_version = table.get("version")
        if not isinstance(table_id, str) or not isinstance(table_version, str):
            raise ConfigHistoryUnavailableError("DeviceModel PointTable reference must be exact")
        table_key = ("point_table", table_id, table_version)
        table_document = objects.get(table_key)
        if table_document is None:
            raise ConfigHistoryUnavailableError("DeviceModel exact PointTable version is absent")
        table_raw = documents[table_key]
        point_table_id = _point_table_identity(table_key, table_raw)
        table_device_type = table_raw.get("device_type")
        if table_device_type is not None and table_device_type != device_type_id:
            raise ConfigHistoryUnavailableError(
                "PointTable device_type disagrees with DeviceModel device_type_id"
            )
        model = DeviceModel(
            config_id=config_id,
            version_id=document.version_id,
            file_version=file_version,
            device_type_id=device_type_id,
            point_table_id=point_table_id,
        )
        previous_model = model_version_ids.get(model.version_id)
        if previous_model is not None and previous_model != model:
            raise ConfigHistoryUnavailableError("DeviceModel version_id was rebound")
        models[(config_id, file_version)] = model
        model_version_ids[model.version_id] = model
        model_point_keys.add(table_key)

    bindings: list[DeviceBinding] = []
    site_sources = {
        (source.config_id, source.file_version)
        for source in sources
        if source.config_type == "site"
    }
    for source in sources:
        if source.config_type != "device":
            continue
        site_config_id = source.site_config_id
        site_id = source.site_id
        site_file_version = source.site_file_version
        if site_config_id is None or site_id is None or site_file_version is None:
            raise ConfigHistoryUnavailableError("device ConfigSource lacks an exact Site pin")
        if (site_config_id, site_file_version) not in site_sources:
            raise ConfigHistoryUnavailableError(
                "device ConfigSource requires an independent exact Site ConfigSource"
            )
        site_key = ("site", site_config_id, site_file_version)
        site_document = objects.get(site_key)
        if site_document is None:
            raise ConfigHistoryUnavailableError("exact Site ConfigSource document is absent")
        site_raw = documents[site_key]
        if (
            site_raw.get("schema_version") != 1
            or type(site_raw.get("schema_version")) is not int
            or site_raw.get("file_version") != site_file_version
            or site_raw.get("site_id") != site_id
        ):
            raise ConfigHistoryUnavailableError("Site document identity is inconsistent")
        device_key = ("device", source.config_id, source.file_version)
        device_document = objects.get(device_key)
        if device_document is None:
            raise ConfigHistoryUnavailableError("exact device ConfigSource document is absent")
        raw = documents[device_key]
        devices = raw.get("devices")
        if (
            raw.get("schema_version") != 1
            or type(raw.get("schema_version")) is not int
            or raw.get("file_version") != source.file_version
            or not isinstance(devices, dict)
        ):
            raise ConfigHistoryUnavailableError("device instance document is malformed")
        for device_id, entry in devices.items():
            if not isinstance(device_id, str) or not device_id or not isinstance(entry, dict):
                raise ConfigHistoryUnavailableError("device instance entry is malformed")
            model_ref = entry.get("model")
            if not isinstance(model_ref, dict):
                raise ConfigHistoryUnavailableError(
                    "device instance requires exact model reference"
                )
            model_id = model_ref.get("id")
            model_version = model_ref.get("version")
            if not isinstance(model_id, str) or not isinstance(model_version, str):
                raise ConfigHistoryUnavailableError("device model reference must be exact")
            bound_model = models.get((model_id, model_version))
            if bound_model is None:
                raise ConfigHistoryUnavailableError(
                    "device instance DeviceModel version is absent from the fixed closure"
                )
            binding = DeviceBinding(
                wind_farm_id=site_id,
                device_id=device_id,
                device_config_id=source.config_id,
                device_version_id=device_document.version_id,
                device_file_version=source.file_version,
                site_config_id=site_config_id,
                site_file_version=site_file_version,
                model=bound_model,
            )
            bindings.append(binding)

    points: dict[str, tuple[str, str]] = {}
    for key, document in objects.items():
        config_type, config_id, file_version = key
        if config_type != "point_table":
            continue
        explicitly_required = file_version in required.get((config_type, config_id), set())
        if not explicitly_required and key not in model_point_keys:
            continue
        point_table_id = _point_table_identity(key, documents[key])
        previous_point = points.get(point_table_id)
        pin = (config_id, document.version_id)
        if previous_point is not None and previous_point != pin:
            raise ConfigHistoryUnavailableError("full PointTable ID was rebound")
        points[point_table_id] = pin
    return points, tuple(bindings)


class ConfigurationGrpcClient:
    """Request exact versions and revision-pinned bundle; never use a latest-version alias."""

    def __init__(
        self,
        *,
        endpoint: str,
        token_env: str,
        archive: ImmutableConfigArchive,
        timeout_s: float = 10.0,
    ) -> None:
        if not endpoint or not token_env or timeout_s <= 0:
            raise ValueError("Configuration client endpoint/token/deadline required")
        self._endpoint = endpoint
        self._token_env = token_env
        self._archive = archive
        self._timeout = timeout_s
        self._channel: grpc.aio.Channel | None = None
        self._stub: rpc.ConfigurationRepositoryServiceAsyncStub | None = None

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        token = os.getenv(self._token_env)
        if not token or len(token.encode("utf-8")) < 32:
            raise ConfigHistoryUnavailableError(
                "Configuration bearer token is missing or too short"
            )
        if any(character in token for character in "\r\n"):
            raise ConfigHistoryUnavailableError(
                "Configuration bearer token contains invalid controls"
            )
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

    def _client(self) -> rpc.ConfigurationRepositoryServiceAsyncStub:
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
            config_type,
            config_id,
            version_id,
        ):
            raise ConfigHistoryUnavailableError("configuration repository returned wrong version")
        await asyncio.to_thread(self._archive.persist, document)
        return document

    async def fetch_service_bundle(self, *, service_id: str) -> StorageBundle:
        """Resolve one authority revision then validate every returned YAML byte."""
        entries: list[pb.ConfigSummary] = []
        page_token = ""
        revision = 0
        seen_tokens: set[str] = set()
        for _page in range(100):
            listing = await self._client().ListConfigs(
                pb.ListConfigsRequest(
                    config_type="service",
                    config_id_prefix=service_id,
                    page_size=100,
                    page_token=page_token,
                ),
                timeout=self._timeout,
                metadata=self._metadata(),
            )
            if listing.snapshot_revision_id <= 0:
                raise ConfigHistoryUnavailableError("service listing has no snapshot revision")
            if revision and listing.snapshot_revision_id != revision:
                raise ConfigHistoryUnavailableError("service listing changed snapshot revision")
            revision = listing.snapshot_revision_id
            entries.extend(listing.items)
            if len(entries) > 10000:
                raise ConfigHistoryUnavailableError("service listing exceeds bounded result size")
            page_token = listing.next_page_token
            if not page_token:
                break
            if page_token in seen_tokens:
                raise ConfigHistoryUnavailableError("service listing repeats a page token")
            seen_tokens.add(page_token)
        else:
            raise ConfigHistoryUnavailableError("service listing exceeds bounded page count")
        roots = [
            entry
            for entry in entries
            if entry.config_id.config_type == "service" and entry.config_id.config_id == service_id
        ]
        if len(roots) != 1 or revision <= 0:
            raise ConfigHistoryUnavailableError("service config absent or revision unspecified")
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
            raise ConfigHistoryUnavailableError("Configuration bundle revision mismatch")
        digest = hashlib.sha256()
        objects: dict[tuple[str, str, str], RepositoryDocument] = {}
        file_versions: dict[tuple[str, str], set[str]] = {}
        for item in response.files:
            digest.update(item.version_id.encode("utf-8"))
            digest.update(item.yaml_sha256.encode("utf-8"))
            document = _document(item)
            key = (document.config_type, document.config_id, item.file_version)
            if key in objects:
                raise ConfigHistoryUnavailableError("duplicate config version in one closure")
            objects[key] = document
            document_identity = (document.config_type, document.config_id)
            file_versions.setdefault(document_identity, set()).add(item.file_version)
        if digest.hexdigest() != response.manifest_sha256:
            raise ConfigHistoryUnavailableError("Configuration bundle manifest SHA256 mismatch")
        if sum(len(d.yaml_bytes) for d in objects.values()) != response.total_bytes:
            raise ConfigHistoryUnavailableError("Configuration bundle byte count mismatch")
        service_entries = [
            document
            for (config_type, config_id, _version), document in objects.items()
            if (config_type, config_id) == ("service", service_id)
        ]
        if len(service_entries) != 1:
            raise ConfigHistoryUnavailableError("service config closure is ambiguous")
        service = service_entries[0]
        if service.version_id != roots[0].recommended_version_id:
            raise ConfigHistoryUnavailableError("service version differs from pinned revision")
        configuration = load_storage_config(service.yaml_bytes)
        if configuration.service_id != service_id:
            raise ConfigHistoryUnavailableError("service config identity mismatch")
        required: dict[tuple[str, str], set[str]] = {}
        for source in configuration.config_sources:
            required.setdefault((source.config_type, source.config_id), set()).add(
                source.file_version
            )
        for required_key, versions in required.items():
            if not versions <= file_versions.get(required_key, set()):
                raise ConfigHistoryUnavailableError(
                    "Configuration bundle omitted an explicitly pinned historical dependency"
                )
        points, device_bindings = _parse_device_closure(objects, configuration.config_sources)
        # Persist only after every model/instance/table relationship validates.
        for document in objects.values():
            await asyncio.to_thread(self._archive.persist, document)
        business = [key for key in objects if key[0] == "business_points" and key[:2] in required]
        if len(business) > 1:
            raise ConfigHistoryUnavailableError("ambiguous business point catalog")
        pin = (business[0][1], objects[business[0]].version_id) if business else None
        return StorageBundle(
            configuration=configuration,
            revision_id=revision,
            point_table_versions=DeviceClosurePins(points, device_bindings),
            business_points_pin=pin,
            device_bindings=device_bindings,
        )
