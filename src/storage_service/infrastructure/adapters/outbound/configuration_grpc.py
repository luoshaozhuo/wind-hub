"""Read-only bearer-authorized Configuration client and immutable bundle loader.

No certificate-based fallback, anonymous gRPC call, or latest-version alias is
allowed. Configuration publishes exact immutable versions for Storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field

import grpc
import yaml

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from core.configuration.deployment import closure_sha256
from storage_service.application.config import (
    DeviceBinding,
    DeviceClosurePins,
    DeviceModel,
    StorageConfig,
    load_storage_config,
)
from storage_service.application.ingestion_contract import ConfigHistoryUnavailableError
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


_YAML_BOOL_PATTERN = re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$")


class _RepositoryYamlLoader(yaml.SafeLoader):
    """Match Configuration's strict YAML scalar and mapping rules."""

    yaml_implicit_resolvers = {
        key: [
            (tag, pattern)
            for tag, pattern in resolvers
            if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
        ]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_RepositoryYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", _YAML_BOOL_PATTERN, list("tTfF")
)


def _repository_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise ConfigHistoryUnavailableError("duplicate or invalid Configuration YAML key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_RepositoryYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _repository_mapping
)


def _validate_yaml_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _validate_yaml_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or any(ch in key for ch in ("\\0", "\\r", "\\n")):
                raise ConfigHistoryUnavailableError("invalid Configuration YAML mapping key")
            _validate_yaml_value(item)
        return
    raise ConfigHistoryUnavailableError("invalid Configuration YAML scalar")


def _yaml_object(document: RepositoryDocument) -> dict[str, object]:
    try:
        source = document.yaml_bytes.decode("utf-8")
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(source)):
            raise ConfigHistoryUnavailableError("Configuration YAML aliases are forbidden")
        values = list(yaml.load_all(source, Loader=_RepositoryYamlLoader))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ConfigHistoryUnavailableError("configuration YAML cannot be parsed") from exc
    if len(values) != 1 or not isinstance(values[0], dict):
        raise ConfigHistoryUnavailableError("configuration document must be one object")
    value: dict[str, object] = values[0]
    _validate_yaml_value(value)
    return value


def _document_content_hash(document: RepositoryDocument) -> str:
    parsed = _yaml_object(document)
    encoded = json.dumps(
        parsed,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    sites = [(key, raw) for key, raw in documents.items() if key[0] == "site" and key[1] == site_id]
    devices = [
        (key, raw) for key, raw in documents.items() if key[0] == "device" and key[1] == devices_id
    ]
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
        model_id = ref.get("id")
        model_version = ref.get("version")
        if not isinstance(model_id, str) or not isinstance(model_version, str):
            raise ConfigHistoryUnavailableError("device model pin must use exact identities")
        model = models.get((model_id, model_version))
        if model is None:
            raise ConfigHistoryUnavailableError("device references absent fixed DeviceModel")
        table_id = model.point_table_id
        point_table_key = next(
            (
                item
                for item in documents
                if item[0] == "point_table"
                and _point_table_identity(item, documents[item]) == table_id
            ),
            None,
        )
        if point_table_key is None:
            raise ConfigHistoryUnavailableError("model PointTable pin is missing")
        pins[table_id] = (point_table_key[1], objects[point_table_key].version_id)
        bindings.append(
            DeviceBinding(
                wind_farm_id=wind_farm_id,
                device_id=device_id,
                device_config_id=device_key[1],
                device_version_id=objects[device_key].version_id,
                device_file_version=device_key[2],
                site_config_id=site_key[1],
                site_file_version=site_key[2],
                model=model,
            )
        )
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

    async def fetch_exact_device_closure(
        self, *, device_config_id: str
    ) -> tuple[RepositoryDocument, ...]:
        """Resolve an archived Device Instance file version without latest selection."""
        match = re.fullmatch(
            r"device_(?P<id>[A-Za-z0-9_.-]+)_v" r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)",
            device_config_id,
        )
        if match is None:
            raise ConfigHistoryUnavailableError("invalid exact Device Instance identity")
        identity = pb.ConfigId(config_type="device", config_id=match["id"])
        response = await self._client().GetConfigBundle(
            pb.GetConfigBundleRequest(
                exact_roots=[
                    pb.ConfigVersionRef(config_id=identity, file_version=match["version"])
                ],
                include_dependencies=True,
            ),
            timeout=self._timeout,
            metadata=self._metadata(),
        )
        if response.revision_id < 1 or not response.files:
            raise ConfigHistoryUnavailableError("exact historical bundle is malformed")
        digest = hashlib.sha256()
        documents: list[RepositoryDocument] = []
        identities: set[tuple[str, str, str]] = set()
        paths: set[str] = set()
        roots = 0
        for item in response.files:
            doc = _document(item)
            key = (doc.config_type, doc.config_id, doc.version_id)
            if key in identities or not item.relative_path or item.relative_path in paths:
                raise ConfigHistoryUnavailableError("historical bundle duplicates a document")
            identities.add(key)
            paths.add(item.relative_path)
            parsed = _yaml_object(doc)
            if (
                type(parsed.get("schema_version")) is not int
                or parsed["schema_version"] != item.schema_version
                or parsed.get("file_version") != item.file_version
                or _document_content_hash(doc) != item.content_sha256
            ):
                raise ConfigHistoryUnavailableError(
                    "historical configuration semantic identity failed validation"
                )
            for value in (item.relative_path, item.version_id, item.yaml_sha256):
                encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(4, "big"))
                digest.update(encoded)
            documents.append(doc)
            if (
                doc.config_type == "device"
                and doc.config_id == match["id"]
                and item.file_version == match["version"]
            ):
                roots += 1
        if (
            roots != 1
            or digest.hexdigest() != response.manifest_sha256
            or sum(len(doc.yaml_bytes) for doc in documents) != response.total_bytes
        ):
            raise ConfigHistoryUnavailableError("historical bundle manifest or root mismatch")
        for doc in documents:
            await asyncio.to_thread(self._archive.persist, doc)
        return tuple(documents)

    async def get_current_revision(self) -> int:
        """Read the authoritative publication cursor, not a recommended version."""
        response = await self._client().GetCurrentRevision(
            pb.GetCurrentRevisionRequest(),
            timeout=self._timeout,
            metadata=self._metadata(),
        )
        if response.revision_id <= 0:
            raise ConfigHistoryUnavailableError("Configuration has no published revision")
        return int(response.revision_id)

    async def fetch_point_table_bundle(
        self, *, point_table_id: str, expected_manifest_sha256: str = ""
    ) -> tuple[RepositoryDocument, ...]:
        """Archive a historical interpretation closure without asserting device ownership."""
        response = await self._client().GetPointTableBundle(
            pb.GetPointTableBundleRequest(
                point_table_id=point_table_id,
                expected_manifest_sha256=expected_manifest_sha256,
            ),
            timeout=self._timeout,
            metadata=self._metadata(),
        )
        if response.point_table_id != point_table_id:
            raise ConfigHistoryUnavailableError("wrong historical PointTable identity")
        items: list[dict[str, str]] = []
        documents: list[RepositoryDocument] = []
        roots = 0
        seen: set[tuple[str, str, str]] = set()
        for item in response.documents:
            doc = _document(item)
            parsed = _yaml_object(doc)
            if (
                type(parsed.get("schema_version")) is not int
                or parsed["schema_version"] != item.schema_version
                or parsed.get("file_version") != item.file_version
            ):
                raise ConfigHistoryUnavailableError("historical document schema identity mismatch")
            if _document_content_hash(doc) != item.content_sha256:
                raise ConfigHistoryUnavailableError("historical semantic content digest mismatch")
            identity = (doc.config_type, doc.config_id, doc.version_id)
            if identity in seen:
                raise ConfigHistoryUnavailableError("duplicate historical document identity")
            seen.add(identity)
            items.append(
                {
                    "config_type": doc.config_type,
                    "config_id": doc.config_id,
                    "version_id": doc.version_id,
                    "content_sha256": item.content_sha256,
                    "yaml_sha256": doc.yaml_sha256,
                    "relative_path": item.relative_path,
                }
            )
            documents.append(doc)
            if (
                doc.config_type == "point_table"
                and point_table_id == f"point_table_{doc.config_id}_v{item.file_version}"
            ):
                _point_table_identity(
                    ("point_table", doc.config_id, item.file_version),
                    _yaml_object(doc),
                )
                roots += 1
        kinds = {doc.config_type for doc in documents}
        if roots != 1 or not {"point_table", "business_points", "units"} <= kinds:
            raise ConfigHistoryUnavailableError("incomplete historical semantic closure")
        items.sort(key=lambda item: (item["config_type"], item["config_id"], item["version_id"]))
        encoded = json.dumps(
            items,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != response.manifest_sha256 or (
            expected_manifest_sha256 and digest != expected_manifest_sha256
        ):
            raise ConfigHistoryUnavailableError("historical semantic manifest mismatch")
        for doc in documents:
            await asyncio.to_thread(self._archive.persist, doc)
        return tuple(documents)

    async def fetch_service_bundle(self, *, deployment_id: str, service_id: str) -> StorageBundle:
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
            for manifest_part in (item.relative_path, item.version_id, item.yaml_sha256):
                data = manifest_part.encode("utf-8")
                manifest.update(len(data).to_bytes(4, "big"))
                manifest.update(data)
            document = _document(item)
            parsed = _yaml_object(document)
            if (
                type(parsed.get("schema_version")) is not int
                or parsed["schema_version"] != item.schema_version
                or parsed.get("file_version") != item.file_version
            ):
                raise ConfigHistoryUnavailableError(
                    "Configuration document schema identity mismatch"
                )
            if _document_content_hash(document) != item.content_sha256:
                raise ConfigHistoryUnavailableError(
                    "Configuration semantic content digest mismatch"
                )
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
        services = [
            doc for key, doc in objects.items() if key[0] == "service" and key[1] == service_id
        ]
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
        if not {item.wind_farm_id for item in bindings} <= set(config.allowed_wind_farms):
            raise ConfigHistoryUnavailableError(
                "selected Device Instances exceed Storage authorized wind farms"
            )
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
