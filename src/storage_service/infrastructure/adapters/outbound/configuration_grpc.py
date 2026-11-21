"""Read-only bearer-authorized Configuration client and immutable bundle loader.

No certificate-based fallback, anonymous gRPC call, or latest-version alias is
allowed. Configuration publishes exact immutable versions for Storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field

import grpc
import yaml

from core.configuration.deployment import closure_sha256

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from storage_service.application.config import (
    DeviceBinding,
    DeviceClosurePins,
    DeviceModel,
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
    deployment_id: str
    target_revision: str
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
    *,
    site_id: str,
    devices_id: str,
) -> tuple[dict[str, tuple[str, str]], tuple[DeviceBinding, ...]]:
    """Resolve the Deployment-selected instances through their pinned models."""
    documents = {
        key: _yaml_object(document)
        for key, document in objects.items()
        if key[0] in {"point_table", "device_model", "device", "site"}
    }
    sites = [(key, raw) for key, raw in documents.items()
             if key[0] == "site" and key[1] == site_id]
    devices = [(key, raw) for key, raw in documents.items()
               if key[0] == "device" and key[1] == devices_id]
    if len(sites) != 1 or len(devices) != 1:
        raise ConfigHistoryUnavailableError(
            "Deployment-selected Site/Device Instance is missing or ambiguous"
        )
    site_key, site_raw = sites[0]
    device_key, device_raw = devices[0]
    wind_farm_id = site_raw.get("site_id")
    if (
        type(site_raw.get("schema_version")) is not int
        or site_raw["schema_version"] != 1
        or site_raw.get("file_version") != site_key[2]
        or not isinstance(wind_farm_id, str)
        or not wind_farm_id
    ):
        raise ConfigHistoryUnavailableError("selected Site identity is invalid")
    entries = device_raw.get("devices")
    if (
        type(device_raw.get("schema_version")) is not int
        or device_raw["schema_version"] != 1
        or device_raw.get("file_version") != device_key[2]
        or not isinstance(entries, dict)
        or not entries
    ):
        raise ConfigHistoryUnavailableError("selected Device Instance is invalid")
    models: dict[tuple[str, str], DeviceModel] = {}
    for key, raw in documents.items():
        if key[0] != "device_model":
            continue
        if (
            type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
            or raw.get("template_id") != key[1]
            or raw.get("file_version") != key[2]
        ):
            raise ConfigHistoryUnavailableError("DeviceModel identity is invalid")
        ref = raw.get("point_table")
        device_type = raw.get("device_type_id")
        if not isinstance(ref, dict) or not isinstance(device_type, str) or not device_type:
            raise ConfigHistoryUnavailableError("DeviceModel semantic reference is invalid")
        table_id, table_version = ref.get("id"), ref.get("version")
        if not isinstance(table_id, str) or not isinstance(table_version, str):
            raise ConfigHistoryUnavailableError("DeviceModel PointTable pin is invalid")
        table_key = ("point_table", table_id, table_version)
        if table_key not in documents:
            raise ConfigHistoryUnavailableError("pinned PointTable is absent")
        table_identity = _point_table_identity(table_key, documents[table_key])
        table_device_type = documents[table_key].get("device_type")
        if table_device_type is not None and table_device_type != device_type:
            raise ConfigHistoryUnavailableError("PointTable device_type conflicts with model")
        models[(key[1], key[2])] = DeviceModel(
            config_id=key[1],
            version_id=objects[key].version_id,
            file_version=key[2],
            device_type_id=device_type,
            point_table_id=table_identity,
        )
    pins: dict[str, tuple[str, str]] = {}
    bindings: list[DeviceBinding] = []
    for device_id, value in entries.items():
        if not isinstance(device_id, str) or not device_id or not isinstance(value, dict):
            raise ConfigHistoryUnavailableError("invalid device in selected Instance")
        ref = value.get("model")
        if not isinstance(ref, dict):
            raise ConfigHistoryUnavailableError("device model pin is missing")
        model = models.get((ref.get("id"), ref.get("version")))
        if model is None:
            raise ConfigHistoryUnavailableError("device references absent fixed DeviceModel")
        table_id = model.point_table_id
        key = next((item for item in documents
                    if item[0] == "point_table"
                    and _point_table_identity(item, documents[item]) == table_id), None)
        if key is None:
            raise ConfigHistoryUnavailableError("model PointTable pin is missing")
        pins[table_id] = (key[1], objects[key].version_id)
        bindings.append(DeviceBinding(
            wind_farm_id=wind_farm_id,
            device_id=device_id,
            device_config_id=device_key[1],
            device_version_id=objects[device_key].version_id,
            device_file_version=device_key[2],
            site_config_id=site_key[1],
            site_file_version=site_key[2],
            model=model,
        ))
    return pins, tuple(bindings)

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

    async def fetch_service_bundle(
        self, *, deployment_id: str, service_id: str
    ) -> StorageBundle:
        """Consume only the effective Deployment-selected service closure."""
        response = await self._client().GetServiceConfigBundle(
            pb.GetServiceConfigBundleRequest(
                deployment_id=deployment_id, service_id=service_id, revision_id=0
            ),
            timeout=self._timeout,
            metadata=self._metadata(),
        )
        revision = response.repository_revision_id
        if revision <= 0 or not response.target_revision:
            raise ConfigHistoryUnavailableError("Configuration returned no authoritative target")
        manifest = hashlib.sha256()
        objects: dict[_ConfigKey, RepositoryDocument] = {}
        closure: list[tuple[str, str, str, str]] = []
        for item in response.files:
            for field in (item.relative_path, item.version_id, item.yaml_sha256):
                data = field.encode("utf-8")
                manifest.update(len(data).to_bytes(4, "big"))
                manifest.update(data)
            document = _document(item)
            if not item.content_sha256 or not item.file_version:
                raise ConfigHistoryUnavailableError("Configuration document is missing identity")
            key = (document.config_type, document.config_id, item.file_version)
            if key in objects:
                raise ConfigHistoryUnavailableError("duplicate identity in service closure")
            if _yaml_object(document).get("file_version") != item.file_version:
                raise ConfigHistoryUnavailableError("document declared version mismatch")
            objects[key] = document
            closure.append((*key, item.content_sha256))
        if manifest.hexdigest() != response.manifest_sha256:
            raise ConfigHistoryUnavailableError("service manifest digest mismatch")
        if sum(len(doc.yaml_bytes) for doc in objects.values()) != response.total_bytes:
            raise ConfigHistoryUnavailableError("service closure byte count mismatch")
        try:
            fingerprint = closure_sha256(deployment_id, service_id, closure)
        except ValueError as exc:
            raise ConfigHistoryUnavailableError("invalid selected service closure") from exc
        if fingerprint != response.target_revision:
            raise ConfigHistoryUnavailableError("selected service closure digest mismatch")
        services = [doc for key, doc in objects.items()
                    if key[0] == "service" and key[1] == service_id]
        if len(services) != 1:
            raise ConfigHistoryUnavailableError("selected Service identity is ambiguous")
        config = load_storage_config(services[0].yaml_bytes)
        if config.service_id != service_id:
            raise ConfigHistoryUnavailableError("selected Service ID disagrees with bootstrap")
        pins, bindings = _parse_device_closure(
            objects,
            site_id=config.config_sources.instances.site,
            devices_id=config.config_sources.instances.devices,
        )
        if not pins or not bindings:
            raise ConfigHistoryUnavailableError("selected service has no device semantics")
        catalogs = [key for key in objects if key[0] == "business_points"]
        if len(catalogs) != 1:
            raise ConfigHistoryUnavailableError("selected BusinessPoint catalog is ambiguous")
        catalog = catalogs[0]
        business_pin = (catalog[1], objects[catalog].version_id)
        for doc in objects.values():
            await asyncio.to_thread(self._archive.persist, doc)
        return StorageBundle(
            configuration=config,
            revision_id=revision,
            deployment_id=deployment_id,
            target_revision=fingerprint,
            point_table_versions=DeviceClosurePins(pins, bindings),
            business_points_pin=business_pin,
            device_bindings=bindings,
        )
