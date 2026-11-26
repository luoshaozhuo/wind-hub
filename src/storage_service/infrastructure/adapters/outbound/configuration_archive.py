"""Configuration Repository fixed-version point table closure and immutable local archive.

Storage never interprets historical Kafka records using the *current* recommended
PointTable. All required IDs must be explicitly pinned by version_id and all
bytes are verified against the repository yaml_sha256 before use.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import grpc
import yaml

from core.domain.protocol_types import normalize_protocol_scalar_type
from core.domain.value_objects import DataType
from storage_service.application.config import (
    DeviceBinding,
    DeviceClosurePins,
    DeviceModel,
    TypeMapping,
)
from storage_service.application.ingestion_contract import ConfigHistoryUnavailableError
from storage_service.domain.wide_record import WideDataLevel, WideIdentity, WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RepositoryDocument:
    """One document returned by the authoritative Configuration Repository."""

    config_type: str
    config_id: str
    version_id: str
    revision_id: int
    yaml_bytes: bytes
    yaml_sha256: str


class DocumentFetcher(Protocol):
    async def fetch_point_table_bundle(
        self, *, point_table_id: str, expected_manifest_sha256: str = ""
    ) -> tuple[RepositoryDocument, ...]:
        """Fetch one immutable historical interpretation bundle."""

    async def fetch_by_version(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> RepositoryDocument:
        """Fetch a single immutable version, not the recommended latest."""

    async def fetch_exact_device_closure(
        self, *, device_config_id: str
    ) -> tuple[RepositoryDocument, ...]:
        """Fetch one full historical Device Instance dependency closure."""


class ImmutableConfigArchive:
    """A content-addressed, fsync-durable archive with no implicit garbage collection."""

    def __init__(self, directory: Path, *, max_bytes: int = 2147483648) -> None:
        if not directory.is_absolute():
            raise ValueError("archive directory must be absolute")
        if max_bytes < 1:
            raise ValueError("archive byte limit must be positive")
        self._directory = directory
        self._directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def _used_bytes(self) -> int:
        used = 0
        for entry in self._directory.rglob("*"):
            if entry.is_symlink():
                raise ConfigHistoryUnavailableError("archive contains a symbolic link")
            if entry.is_file():
                used += entry.stat().st_size
            elif not entry.is_dir():
                raise ConfigHistoryUnavailableError("archive contains an unsafe file")
        return used

    def set_max_bytes(self, value: int) -> None:
        """Apply the configured cache budget before accepting live Kafka records."""
        if value < 1:
            raise ValueError("archive byte limit must be positive")
        with self._lock:
            if self._used_bytes() > value:
                raise ConfigHistoryUnavailableError(
                    "archive already exceeds configured cache limit"
                )
            self._max_bytes = value

    def _matching_manifests(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> list[tuple[Path, list[Any]]]:
        matches: list[tuple[Path, list[Any]]] = []
        for manifest_path in self._directory.glob("*/manifest.json"):
            try:
                fields = json.loads(manifest_path.read_bytes())
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ConfigHistoryUnavailableError("malformed historical archive") from exc
            if (
                isinstance(fields, list)
                and len(fields) == 5
                and fields[:3] == [config_type, config_id, version_id]
            ):
                matches.append((manifest_path, fields))
        return matches

    def persist(self, document: RepositoryDocument) -> None:
        digest = hashlib.sha256(document.yaml_bytes).hexdigest()
        if not _HEX64.fullmatch(document.yaml_sha256) or digest != document.yaml_sha256:
            raise ConfigHistoryUnavailableError("Configuration YAML SHA256 mismatch")
        if not all((document.config_type, document.config_id, document.version_id)):
            raise ConfigHistoryUnavailableError("configuration document identity is missing")

        manifest_fields = [
            document.config_type,
            document.config_id,
            document.version_id,
            document.revision_id,
            document.yaml_sha256,
        ]
        manifest = json.dumps(
            manifest_fields,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        stable_identity = json.dumps(
            [
                document.config_type,
                document.config_id,
                document.version_id,
                document.yaml_sha256,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        name = hashlib.sha256(stable_identity).hexdigest()
        item_dir = self._directory / name
        with self._lock:
            existing = self._matching_manifests(
                config_type=document.config_type,
                config_id=document.config_id,
                version_id=document.version_id,
            )
            if any(
                fields[:3] != manifest_fields[:3] or fields[4] != manifest_fields[4]
                for _, fields in existing
            ):
                raise ConfigHistoryUnavailableError(
                    "immutable configuration identity was rebound to different content"
                )
            expected = (
                (item_dir / "document.yaml", document.yaml_bytes),
                (item_dir / "manifest.json", manifest),
            )
            added = sum(len(payload) for path, payload in expected if not path.exists())
            if self._used_bytes() + added > self._max_bytes:
                raise ConfigHistoryUnavailableError(
                    "immutable configuration archive quota exceeded"
                )
            item_dir.mkdir(mode=0o700, exist_ok=True)
            for path, payload in expected:
                if path.name == "manifest.json" and path.exists():
                    continue
                self._write_once(path, payload)
            fd = os.open(item_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _write_once(path: Path, payload: bytes) -> None:
        if path.exists():
            if path.read_bytes() != payload:
                raise ConfigHistoryUnavailableError("content-addressed file was modified")
            return
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".partial-", delete=False
            ) as stream:
                temp_name = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp_name, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ConfigHistoryUnavailableError("archive collision") from None
        finally:
            if temp_name is not None:
                os.unlink(temp_name)

    def snapshot_for_backup(
        self, *, destination: Path, max_bytes: int
    ) -> tuple[tuple[str, str, str, str], ...]:
        """Copy and verify every immutable historical YAML before native backup publication."""
        if max_bytes < 1 or destination.exists() or destination.is_symlink():
            raise ValueError("historical backup target must be absent and quota positive")
        identities: list[tuple[str, str, str, str]] = []
        with self._lock:
            if self._directory.is_symlink():
                raise ConfigHistoryUnavailableError("historical archive root is a symlink")
            destination.mkdir(mode=0o700)
            used = 0
            for item in sorted(self._directory.iterdir()):
                if (
                    item.is_symlink()
                    or not item.is_dir()
                    or _HEX64.fullmatch(item.name) is None
                    or {entry.name for entry in item.iterdir()}
                    != {"document.yaml", "manifest.json"}
                ):
                    raise ConfigHistoryUnavailableError("historical backup archive is unsafe")
                if any(
                    candidate.is_symlink() or not candidate.is_file()
                    for candidate in item.iterdir()
                ):
                    raise ConfigHistoryUnavailableError("historical backup includes unsafe entry")
                manifest_bytes = (item / "manifest.json").read_bytes()
                document_bytes = (item / "document.yaml").read_bytes()
                try:
                    fields = json.loads(manifest_bytes)
                except (ValueError, TypeError) as exc:
                    raise ConfigHistoryUnavailableError(
                        "historical backup manifest is malformed"
                    ) from exc
                if (
                    not isinstance(fields, list)
                    or len(fields) != 5
                    or any(
                        not isinstance(value, str) or not value
                        for value in (fields[0], fields[1], fields[2], fields[4])
                    )
                    or type(fields[3]) is not int
                    or fields[3] < 0
                    or _HEX64.fullmatch(fields[4]) is None
                    or hashlib.sha256(document_bytes).hexdigest() != fields[4]
                ):
                    raise ConfigHistoryUnavailableError(
                        "historical backup contains a corrupt immutable document"
                    )
                stable_identity = json.dumps(
                    [fields[0], fields[1], fields[2], fields[4]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if item.name != hashlib.sha256(stable_identity).hexdigest():
                    raise ConfigHistoryUnavailableError(
                        "historical backup archive identity does not match content"
                    )
                used += len(manifest_bytes) + len(document_bytes)
                if used > max_bytes:
                    raise ConfigHistoryUnavailableError(
                        "historical backup exceeds configured byte budget"
                    )
                target = destination / item.name
                target.mkdir(mode=0o700)
                for filename, content in (
                    ("document.yaml", document_bytes),
                    ("manifest.json", manifest_bytes),
                ):
                    with (target / filename).open("xb") as file:
                        os.fchmod(file.fileno(), 0o600)
                        file.write(content)
                        file.flush()
                        os.fsync(file.fileno())
                directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                identities.append((fields[0], fields[1], fields[2], fields[4]))
            directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return tuple(identities)

    def load(self, *, config_type: str, config_id: str, version_id: str) -> RepositoryDocument:
        # An archive entry is not trusted merely because it exists on disk.
        matches = self._matching_manifests(
            config_type=config_type, config_id=config_id, version_id=version_id
        )
        if len({fields[4] for _, fields in matches}) > 1:
            raise ConfigHistoryUnavailableError("ambiguous immutable configuration identity")
        if matches:
            manifest_path, fields = matches[0]
            try:
                content = (manifest_path.parent / "document.yaml").read_bytes()
                if hashlib.sha256(content).hexdigest() != fields[4]:
                    raise ConfigHistoryUnavailableError("corrupted immutable YAML archive")
                stable_manifest = json.dumps(
                    [fields[0], fields[1], fields[2], fields[4]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                legacy_manifest = json.dumps(
                    fields, ensure_ascii=False, separators=(",", ":")
                ).encode()
                valid_names = {
                    hashlib.sha256(stable_manifest).hexdigest(),
                    hashlib.sha256(legacy_manifest).hexdigest(),
                }
                if manifest_path.parent.name not in valid_names:
                    raise ConfigHistoryUnavailableError("corrupted manifest identity")
                return RepositoryDocument(
                    config_type=config_type,
                    config_id=config_id,
                    version_id=version_id,
                    revision_id=int(fields[3]),
                    yaml_bytes=content,
                    yaml_sha256=fields[4],
                )
            except (OSError, TypeError, ValueError) as exc:
                raise ConfigHistoryUnavailableError("malformed historical archive") from exc
        raise ConfigHistoryUnavailableError(
            f"historical configuration unavailable: {config_type}:{config_id}:{version_id}"
        )


def _index_device_bindings(
    bindings: Iterable[DeviceBinding],
) -> dict[tuple[str, str, str], tuple[DeviceBinding, ...]]:
    grouped: dict[tuple[str, str, str], list[DeviceBinding]] = {}
    model_versions: dict[str, object] = {}
    instance_versions: dict[tuple[str, str, str], DeviceBinding] = {}
    for binding in bindings:
        previous_model = model_versions.setdefault(binding.model.version_id, binding.model)
        if previous_model != binding.model:
            raise ConfigHistoryUnavailableError("DeviceModel version was rebound")
        instance_key = (binding.device_version_id, binding.wind_farm_id, binding.device_id)
        previous_instance = instance_versions.setdefault(instance_key, binding)
        if previous_instance != binding:
            raise ConfigHistoryUnavailableError("device instance version was rebound")
        key = (binding.wind_farm_id, binding.device_id, binding.point_table_id)
        candidates = grouped.setdefault(key, [])
        if binding not in candidates:
            candidates.append(binding)
    return {key: tuple(values) for key, values in grouped.items()}


class ConfigurationPointTableResolver:
    """Fail closed unless exact pinned point/semantic versions validate a sample."""

    def __init__(
        self,
        *,
        fetcher: DocumentFetcher,
        archive: ImmutableConfigArchive,
        point_table_versions: Mapping[str, tuple[str, str]],
        business_points_version: tuple[str, str] | None = None,
        historical_pins: Mapping[str, tuple[tuple[str, str], tuple[str, str] | None]] | None = None,
        device_bindings: Iterable[DeviceBinding] | None = None,
        persist_historical: Callable[
            [dict[str, tuple[str, str]], tuple[str, str] | None, DeviceBinding],
            None,
        ]
        | None = None,
    ) -> None:
        if not point_table_versions:
            raise ValueError("explicitly pinned point table versions are required")
        self._fetcher = fetcher
        self._archive = archive
        self._persist_historical = persist_historical
        if device_bindings is None and isinstance(point_table_versions, DeviceClosurePins):
            device_bindings = point_table_versions.device_bindings
        self._device_bindings = _index_device_bindings(device_bindings or ())
        self._pins = dict(point_table_versions)
        self._business_pins = {
            table_id: business_points_version for table_id in point_table_versions
        }
        for table_id, (pinned, semantic_pin) in (historical_pins or {}).items():
            old = self._pins.get(table_id)
            if old is not None and (old != pinned or self._business_pins[table_id] != semantic_pin):
                raise ConfigHistoryUnavailableError(
                    "historical PointTable semantic closure conflicts with active bundle"
                )
            self._pins[table_id] = pinned
            self._business_pins[table_id] = semantic_pin
        self._loaded: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        # Immutable version identities make bounded lookup/schema reuse safe.
        self._exact_binding_cache: dict[tuple[str, str, str], DeviceBinding] = {}
        self._wide_schema_cache: dict[
            tuple[str, WideDataLevel, int, tuple[tuple[str, str], ...]], WideSchema
        ] = {}
        self._lock = asyncio.Lock()
        self._refresh_hook: Callable[[], Awaitable[None]] | None = None

    def set_refresh_hook(self, refresh: Callable[[], Awaitable[None]]) -> None:
        """Enable one authoritative reconciliation before rejecting unknown IDs."""
        self._refresh_hook = refresh

    def validate_immutable_pins(
        self,
        *,
        point_table_versions: Mapping[str, tuple[str, str]],
        business_points_pin: tuple[str, str] | None,
        device_bindings: Iterable[DeviceBinding] | None = None,
    ) -> None:
        """Preflight an immutable closure without modifying active state."""
        if device_bindings is None and isinstance(point_table_versions, DeviceClosurePins):
            device_bindings = point_table_versions.device_bindings
        current_bindings = (
            binding for candidates in self._device_bindings.values() for binding in candidates
        )
        _index_device_bindings((*current_bindings, *(device_bindings or ())))
        for point_table_id, pin in point_table_versions.items():
            old = self._pins.get(point_table_id)
            if old is not None and (
                old != pin or self._business_pins[point_table_id] != business_points_pin
            ):
                raise ConfigHistoryUnavailableError(
                    "existing PointTable ID was rebound to a different immutable version"
                )

    def add_immutable_pins(
        self,
        *,
        point_table_versions: Mapping[str, tuple[str, str]],
        business_points_pin: tuple[str, str] | None,
        device_bindings: Iterable[DeviceBinding] | None = None,
    ) -> None:
        """Apply additions only after validating the complete immutable closure."""
        self.validate_immutable_pins(
            point_table_versions=point_table_versions,
            business_points_pin=business_points_pin,
            device_bindings=device_bindings,
        )
        if device_bindings is None and isinstance(point_table_versions, DeviceClosurePins):
            device_bindings = point_table_versions.device_bindings
        current_bindings = (
            binding for candidates in self._device_bindings.values() for binding in candidates
        )
        merged_device_bindings = _index_device_bindings(
            (*current_bindings, *(device_bindings or ()))
        )
        for point_table_id, pin in point_table_versions.items():
            old = self._pins.get(point_table_id)
            if old is not None and (
                old != pin or self._business_pins[point_table_id] != business_points_pin
            ):
                raise ConfigHistoryUnavailableError(
                    "existing PointTable ID was rebound to a different immutable version"
                )
        for point_table_id, pin in point_table_versions.items():
            self._pins[point_table_id] = pin
            self._business_pins[point_table_id] = business_points_pin
        self._device_bindings = merged_device_bindings
        self._exact_binding_cache.clear()
        self._wide_schema_cache.clear()

    async def _load(
        self,
        *,
        config_type: str,
        config_id: str,
        version_id: str,
        point_table_id: str | None = None,
    ) -> Mapping[str, Any]:
        key = (config_type, config_id, version_id)
        if key not in self._loaded:
            async with self._lock:
                if key not in self._loaded:
                    try:
                        if config_type == "point_table":
                            if point_table_id is None:
                                raise ConfigHistoryUnavailableError(
                                    "historical PointTable identity is required"
                                )
                            bundle = await self._fetcher.fetch_point_table_bundle(
                                point_table_id=point_table_id
                            )
                            matched = [
                                item
                                for item in bundle
                                if (item.config_type, item.config_id, item.version_id) == key
                            ]
                            if len(matched) != 1:
                                raise ConfigHistoryUnavailableError(
                                    "historical bundle does not contain the fixed "
                                    "PointTable version"
                                )
                            document = matched[0]
                        else:
                            document = await self._fetcher.fetch_by_version(
                                config_type=config_type,
                                config_id=config_id,
                                version_id=version_id,
                            )
                        if (document.config_type, document.config_id, document.version_id) != key:
                            raise ConfigHistoryUnavailableError("repository returned wrong version")
                        await asyncio.to_thread(self._archive.persist, document)
                    except (OSError, ConnectionError, TimeoutError):
                        document = await asyncio.to_thread(
                            self._archive.load,
                            config_type=config_type,
                            config_id=config_id,
                            version_id=version_id,
                        )
                    except grpc.RpcError as exc:
                        if exc.code() not in (
                            grpc.StatusCode.UNAVAILABLE,
                            grpc.StatusCode.DEADLINE_EXCEEDED,
                            grpc.StatusCode.NOT_FOUND,
                        ):
                            raise
                        document = await asyncio.to_thread(
                            self._archive.load,
                            config_type=config_type,
                            config_id=config_id,
                            version_id=version_id,
                        )
                    if hashlib.sha256(document.yaml_bytes).hexdigest() != document.yaml_sha256:
                        raise ConfigHistoryUnavailableError("YAML digest invalid")
                    parsed: Any = yaml.safe_load(document.yaml_bytes)
                    if not isinstance(parsed, dict):
                        raise ConfigHistoryUnavailableError("historical config is not an object")
                    self._loaded[key] = parsed
        return self._loaded[key]

    @staticmethod
    def _require_table_identity(
        *,
        point_table_id: str,
        config_id: str,
        document: Mapping[str, Any],
        device_type_id: str | None = None,
    ) -> None:
        template_id = document.get("template_id")
        file_version = document.get("file_version")
        schema_version = document.get("schema_version")
        if (
            template_id != config_id
            or type(schema_version) is not int
            or schema_version != 1
            or not isinstance(file_version, str)
            or point_table_id != f"point_table_{template_id}_v{file_version}"
        ):
            raise ConfigHistoryUnavailableError(
                "pinned PointTable YAML identity is inconsistent with Kafka identity"
            )
        declared_type = document.get("device_type")
        if (
            declared_type is not None
            and device_type_id is not None
            and declared_type != device_type_id
        ):
            raise ConfigHistoryUnavailableError(
                "PointTable device_type disagrees with authoritative DeviceModel"
            )

    @staticmethod
    def _parse_exact_device_binding(
        identity: WideIdentity | WideRecord,
        documents: tuple[RepositoryDocument, ...],
    ) -> tuple[
        DeviceBinding,
        tuple[str, str],
        tuple[str, str] | None,
    ]:
        """Rebuild immutable Device → Model → PointTable semantic pins from YAML."""
        parsed: dict[tuple[str, str, str], tuple[RepositoryDocument, dict[str, Any]]] = {}
        for document in documents:
            if hashlib.sha256(document.yaml_bytes).hexdigest() != document.yaml_sha256:
                raise ConfigHistoryUnavailableError("historical YAML checksum mismatch")
            raw = yaml.safe_load(document.yaml_bytes)
            if not isinstance(raw, dict):
                raise ConfigHistoryUnavailableError("historical configuration YAML is invalid")
            version = raw.get("file_version")
            if type(raw.get("schema_version")) is not int or not isinstance(version, str):
                raise ConfigHistoryUnavailableError("historical schema/version metadata is absent")
            key = (document.config_type, document.config_id, version)
            if key in parsed:
                raise ConfigHistoryUnavailableError("historical closure contains duplicate version")
            parsed[key] = (document, raw)

        name = re.fullmatch(
            r"device_(?P<id>[A-Za-z0-9_.-]+)_v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)",
            identity.device_config_id,
        )
        if name is None:
            raise ConfigHistoryUnavailableError("invalid historical Device Instance name")
        root = parsed.get(("device", name["id"], name["version"]))
        if root is None:
            raise ConfigHistoryUnavailableError("historical Device Instance root not found")
        device_document, root_yaml = root
        devices = root_yaml.get("devices")
        if not isinstance(devices, dict) or identity.device_id not in devices:
            raise ConfigHistoryUnavailableError("historical device is absent from its Instance")
        selected = devices[identity.device_id]
        if not isinstance(selected, dict) or not isinstance(selected.get("model"), dict):
            raise ConfigHistoryUnavailableError("historical device Model reference absent")
        model_ref = selected["model"]
        model_id, model_version = model_ref.get("id"), model_ref.get("version")
        if not isinstance(model_id, str) or not isinstance(model_version, str):
            raise ConfigHistoryUnavailableError("historical DeviceModel pin is invalid")
        model_entry = parsed.get(("device_model", model_id, model_version))
        if model_entry is None:
            raise ConfigHistoryUnavailableError("historical DeviceModel dependency absent")
        model_document, model_yaml = model_entry
        table_ref = model_yaml.get("point_table")
        device_type = model_yaml.get("device_type_id")
        if (
            model_yaml.get("template_id") != model_id
            or not isinstance(table_ref, dict)
            or not isinstance(device_type, str)
            or not device_type
        ):
            raise ConfigHistoryUnavailableError("historical DeviceModel definition invalid")
        table_id, table_version = table_ref.get("id"), table_ref.get("version")
        if not isinstance(table_id, str) or not isinstance(table_version, str):
            raise ConfigHistoryUnavailableError("historical PointTable pin invalid")
        table_entry = parsed.get(("point_table", table_id, table_version))
        if table_entry is None:
            raise ConfigHistoryUnavailableError("historical PointTable dependency absent")
        table_document, table_yaml = table_entry
        full_table_id = f"point_table_{table_id}_v{table_version}"
        ConfigurationPointTableResolver._require_table_identity(
            point_table_id=full_table_id,
            config_id=table_id,
            document=table_yaml,
            device_type_id=device_type,
        )
        sources = table_yaml.get("config_sources")
        if not isinstance(sources, list):
            raise ConfigHistoryUnavailableError("historical PointTable sources unavailable")
        business_pin: tuple[str, str] | None = None
        for ref in sources:
            if not isinstance(ref, dict):
                raise ConfigHistoryUnavailableError("invalid historical semantic reference")
            kind, name_ref, version_ref = (
                ref.get("config_type"),
                ref.get("config_id"),
                ref.get("file_version"),
            )
            if (
                not isinstance(kind, str)
                or not kind
                or not isinstance(name_ref, str)
                or not name_ref
                or not isinstance(version_ref, str)
                or not version_ref
            ):
                raise ConfigHistoryUnavailableError("historical semantic pin incomplete")
            target = parsed.get((kind, name_ref, version_ref))
            if target is None:
                raise ConfigHistoryUnavailableError("historical semantic dependency missing")
            if kind == "business_points":
                if business_pin is not None:
                    raise ConfigHistoryUnavailableError("ambiguous BusinessPoint catalog")
                business_pin = (name_ref, target[0].version_id)
        model = DeviceModel(
            config_id=model_id,
            version_id=model_document.version_id,
            file_version=model_version,
            device_type_id=device_type,
            point_table_id=full_table_id,
        )
        binding = DeviceBinding(
            wind_farm_id=identity.wind_farm_id,
            device_id=identity.device_id,
            device_config_id=name["id"],
            device_version_id=device_document.version_id,
            device_file_version=name["version"],
            site_config_id="",
            site_file_version="",
            model=model,
        )
        return binding, (table_id, table_document.version_id), business_pin

    async def _fetch_unknown_device(self, identity: WideIdentity | WideRecord) -> None:
        """Atomically validate and durably pin one previously unseen historical device."""
        async with self._lock:
            try:
                self._wide_binding(identity)
                return
            except ConfigHistoryUnavailableError:
                pass
            documents = await self._fetcher.fetch_exact_device_closure(
                device_config_id=identity.device_config_id,
            )
            binding, pin, business = self._parse_exact_device_binding(identity, documents)
            pins = {binding.point_table_id: pin}
            self.validate_immutable_pins(
                point_table_versions=pins,
                business_points_pin=business,
                device_bindings=(binding,),
            )
            if self._persist_historical is None:
                raise ConfigHistoryUnavailableError(
                    "Storage has no durable historical pin registration"
                )
            await asyncio.to_thread(self._persist_historical, pins, business, binding)
            self.add_immutable_pins(
                point_table_versions=pins,
                business_points_pin=business,
                device_bindings=(binding,),
            )

    def _wide_binding(self, identity: WideIdentity | WideRecord) -> DeviceBinding:
        """Resolve one immutable instance identity without recommendation lookup."""
        cache_key = (identity.wind_farm_id, identity.device_id, identity.device_config_id)
        cached = self._exact_binding_cache.get(cache_key)
        if cached is not None:
            return cached
        matches = {
            (binding.device_version_id, binding.point_table_id): binding
            for (farm, device, _), entries in self._device_bindings.items()
            if farm == identity.wind_farm_id and device == identity.device_id
            for binding in entries
            if (
                f"device_{binding.device_config_id}_v{binding.device_file_version}"
                == identity.device_config_id
            )
        }
        if len(matches) != 1:
            raise ConfigHistoryUnavailableError(
                "Kafka Device Instance version lacks one exact historical binding"
            )
        binding = next(iter(matches.values()))
        if len(self._exact_binding_cache) >= 1024:
            self._exact_binding_cache.clear()
        self._exact_binding_cache[cache_key] = binding
        return binding

    def list_wide_identities(
        self,
        *,
        wind_farm_id: str,
        device_ids: tuple[str, ...],
        point_table_id: str,
        device_type: str,
        data_level: WideDataLevel,
    ) -> tuple[WideIdentity, ...]:
        """Enumerate pinned historical device versions for grouped wide reads."""
        if not wind_farm_id:
            raise ValueError("wide history requires an explicit wind farm")
        if len(set(device_ids)) != len(device_ids) or any(not item for item in device_ids):
            raise ValueError("wide history device selector is invalid")
        selected = set(device_ids)
        identities = {
            (
                binding.wind_farm_id,
                binding.device_id,
                f"device_{binding.device_config_id}_v{binding.device_file_version}",
            )
            for candidates in self._device_bindings.values()
            for binding in candidates
            if binding.wind_farm_id == wind_farm_id
            and (not selected or binding.device_id in selected)
            and (not point_table_id or binding.point_table_id == point_table_id)
            and (not device_type or binding.device_type_id == device_type)
        }
        return tuple(
            WideIdentity(farm, config_id, device, data_level)
            for farm, device, config_id in sorted(identities)
        )

    def wide_device_type(self, identity: WideIdentity | WideRecord) -> str:
        """Return the authoritative pinned DeviceModel type for a Kafka identity."""
        return self._wide_binding(identity).device_type_id

    async def resolve_wide_schema(
        self,
        record: WideRecord | WideIdentity,
        *,
        type_mapping: TypeMapping,
        max_row_bytes: int,
    ) -> WideSchema:
        """Resolve an exact Device Instance → Model → PointTable → BusinessPoint schema.

        No Kafka value or current recommendation determines the physical columns.
        """
        try:
            binding = self._wide_binding(record)
        except ConfigHistoryUnavailableError:
            if self._refresh_hook is not None:
                await self._refresh_hook()
            try:
                binding = self._wide_binding(record)
            except ConfigHistoryUnavailableError:
                await self._fetch_unknown_device(record)
                binding = self._wide_binding(record)
        pin = self._pins.get(binding.point_table_id)
        if pin is None:
            raise ConfigHistoryUnavailableError("PointTable lacks an immutable pin")
        mapping = type_mapping.model_dump()
        schema_key = (
            binding.point_table_id,
            record.data_level,
            max_row_bytes,
            tuple(sorted((str(key), str(value)) for key, value in mapping.items())),
        )
        cached_schema = self._wide_schema_cache.get(schema_key)
        if cached_schema is not None:
            return cached_schema
        document = await self._load(
            config_type="point_table",
            config_id=pin[0],
            version_id=pin[1],
            point_table_id=binding.point_table_id,
        )
        self._require_table_identity(
            point_table_id=binding.point_table_id,
            config_id=pin[0],
            document=document,
            device_type_id=binding.device_type_id,
        )
        points = document.get("points")
        protocol = document.get("protocol")
        if not isinstance(points, dict) or not points or not isinstance(protocol, str):
            raise ConfigHistoryUnavailableError("PointTable definitions are incomplete")
        business: Mapping[str, Any] = {}
        if record.data_level is WideDataLevel.BUSINESS:
            catalog_pin = self._business_pins.get(binding.point_table_id)
            if catalog_pin is None:
                raise ConfigHistoryUnavailableError("BUSINESS lacks historical catalog pin")
            catalog = await self._load(
                config_type="business_points",
                config_id=catalog_pin[0],
                version_id=catalog_pin[1],
            )
            entries = catalog.get("business_points")
            if not isinstance(entries, dict):
                raise ConfigHistoryUnavailableError("BusinessPoint catalog is malformed")
            business = entries

        physical: list[PhysicalPoint] = []
        seen_business: set[str] = set()
        for point_id, raw in points.items():
            if not isinstance(point_id, str) or not isinstance(raw, dict):
                raise ConfigHistoryUnavailableError("invalid source point definition")
            logical_id = point_id
            if record.data_level is WideDataLevel.BUSINESS:
                business_id = raw.get("business_point_id")
                if not isinstance(business_id, str) or not business_id:
                    raise ConfigHistoryUnavailableError("source point lacks BusinessPoint")
                if business_id in seen_business:
                    raise ConfigHistoryUnavailableError("BusinessPoint mapping is not one-to-one")
                seen_business.add(business_id)
                definition = business.get(business_id)
                if not isinstance(definition, dict):
                    raise ConfigHistoryUnavailableError("BusinessPoint definition missing")
                raw_type = definition.get("data_type")
                logical_id = business_id
            else:
                raw_type = raw.get("data_type")
            if not isinstance(raw_type, str):
                raise ConfigHistoryUnavailableError("point declared type is absent")
            if record.data_level is WideDataLevel.BUSINESS:
                try:
                    standard = DataType(raw_type.lower())
                except ValueError as exc:
                    raise ConfigHistoryUnavailableError(
                        "BusinessPoint declared type is invalid"
                    ) from exc
            else:
                standard = normalize_protocol_scalar_type(protocol, raw_type)
            key = "string" if standard.value == "str" else standard.value
            sql_type = str(mapping[key])
            address = raw.get("address")
            if record.data_level is WideDataLevel.RAW:
                if not isinstance(address, dict):
                    raise ConfigHistoryUnavailableError("RAW requires protocol address")
                if protocol == "ads":
                    raw_width = address.get("size")
                elif protocol == "modbus":
                    count = address.get("count")
                    raw_width = count * 2 if type(count) is int and count > 0 else None
                else:
                    raw_width = address.get("raw_max_bytes")
                if type(raw_width) is not int or not 0 < raw_width <= 16374:
                    raise ConfigHistoryUnavailableError("RAW width is not trustworthy")
                sql_type = f"VARBINARY({raw_width})"
            elif sql_type in ("VARCHAR", "VARBINARY"):
                if not isinstance(address, dict):
                    raise ConfigHistoryUnavailableError("variable-width point lacks address")
                raw_width = address.get("size", address.get("max_length"))
                if type(raw_width) is not int or not 0 < raw_width <= 16374:
                    raise ConfigHistoryUnavailableError("physical string width unavailable")
                sql_type = f"{sql_type}({raw_width})"
            physical.append(PhysicalPoint(logical_id, sql_type))
        try:
            schema = WideSchema.build(
                point_table_id=binding.point_table_id,
                point_table_version=document["file_version"],
                data_level=record.data_level,
                points=tuple(physical),
                max_row_bytes=max_row_bytes,
            )
            if len(self._wide_schema_cache) >= 256:
                self._wide_schema_cache.clear()
            self._wide_schema_cache[schema_key] = schema
            return schema
        except (KeyError, ValueError) as exc:
            raise ConfigHistoryUnavailableError("historical wide schema is invalid") from exc

    async def get_wide_column_metadata(
        self,
        *,
        point_table_id: str,
        physical_point_id: str,
        data_level: WideDataLevel,
    ) -> tuple[str, str, str, str]:
        """Return source point, BusinessPoint, native type and unit from fixed pins."""
        pin = self._pins.get(point_table_id)
        if pin is None:
            raise ConfigHistoryUnavailableError("wide column lacks historical PointTable pin")
        document = await self._load(
            config_type="point_table",
            config_id=pin[0],
            version_id=pin[1],
            point_table_id=point_table_id,
        )
        self._require_table_identity(
            point_table_id=point_table_id,
            config_id=pin[0],
            document=document,
        )
        points = document.get("points")
        if not isinstance(points, dict):
            raise ConfigHistoryUnavailableError("historical PointTable points unavailable")
        if data_level is WideDataLevel.BUSINESS:
            sources = [
                (point_id, point)
                for point_id, point in points.items()
                if isinstance(point, dict)
                and point.get("business_point_id") == physical_point_id
            ]
            if len(sources) != 1:
                raise ConfigHistoryUnavailableError("BusinessPoint has no unique source point")
            source_point_id, _source = sources[0]
            semantic_pin = self._business_pins.get(point_table_id)
            if semantic_pin is None:
                raise ConfigHistoryUnavailableError("historical BusinessPoint catalog is absent")
            catalog = await self._load(
                config_type="business_points",
                config_id=semantic_pin[0],
                version_id=semantic_pin[1],
            )
            definitions = catalog.get("business_points")
            definition = (
                definitions.get(physical_point_id) if isinstance(definitions, dict) else None
            )
            if not isinstance(definition, dict):
                raise ConfigHistoryUnavailableError("historical BusinessPoint is absent")
            native_type, unit = definition.get("data_type"), definition.get("unit")
            business_id = physical_point_id
        else:
            source_point_id = physical_point_id
            source = points.get(source_point_id)
            if not isinstance(source, dict):
                raise ConfigHistoryUnavailableError("historical source point is absent")
            business_id = source.get("business_point_id") or ""
            native_type = "bytes" if data_level is WideDataLevel.RAW else source.get("data_type")
            unit = "" if data_level is WideDataLevel.RAW else source.get("unit")
        if (
            not isinstance(source_point_id, str)
            or not source_point_id
            or not isinstance(business_id, str)
            or not isinstance(native_type, str)
            or not native_type
            or not isinstance(unit, str)
        ):
            raise ConfigHistoryUnavailableError("historical column metadata is invalid")
        return source_point_id, business_id, native_type, unit

    async def get_wide_unit(
        self,
        *,
        point_table_id: str,
        point_id: str,
        data_level: WideDataLevel,
    ) -> str:
        """Resolve a physical wide column's exact historical unit."""
        if data_level is WideDataLevel.RAW:
            return ""
        if data_level is WideDataLevel.PROTOCOL:
            pin = self._pins.get(point_table_id)
            if pin is None:
                raise ConfigHistoryUnavailableError(
                    "PROTOCOL column lacks an immutable PointTable version"
                )
            config_id, version_id = pin
            document = await self._load(
                config_type="point_table",
                config_id=config_id,
                version_id=version_id,
                point_table_id=point_table_id,
            )
            self._require_table_identity(
                point_table_id=point_table_id,
                config_id=config_id,
                document=document,
            )
            points = document.get("points")
            if not isinstance(points, dict):
                raise ConfigHistoryUnavailableError("pinned PointTable points are malformed")
            point = points.get(point_id)
            if not isinstance(point, dict):
                raise ConfigHistoryUnavailableError("PROTOCOL column has no pinned definition")
            unit = point.get("unit")
            if not isinstance(unit, str) or not unit:
                raise ConfigHistoryUnavailableError("PROTOCOL unit is not pinned")
            return unit
        pin = self._business_pins.get(point_table_id)
        if pin is None:
            raise ConfigHistoryUnavailableError(
                "BUSINESS wide column lacks an immutable semantic catalog"
            )
        document = await self._load(
            config_type="business_points",
            config_id=pin[0],
            version_id=pin[1],
        )
        catalog = document.get("business_points")
        if not isinstance(catalog, dict):
            raise ConfigHistoryUnavailableError("pinned BusinessPoint catalog is malformed")
        definition = catalog.get(point_id)
        if not isinstance(definition, dict):
            raise ConfigHistoryUnavailableError("wide BusinessPoint unit is unavailable")
        unit = definition.get("unit")
        if not isinstance(unit, str) or not unit:
            raise ConfigHistoryUnavailableError("wide BusinessPoint unit must be nonempty")
        return unit
