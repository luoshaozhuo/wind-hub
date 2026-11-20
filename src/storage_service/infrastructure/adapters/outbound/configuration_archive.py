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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import grpc
import yaml

from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.domain.sample import DataLevel, StorageSample

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
    async def fetch_by_version(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> RepositoryDocument:
        """Fetch a single immutable version, not the recommended latest."""


class ImmutableConfigArchive:
    """A content-addressed, fsync-durable archive with no implicit garbage collection."""

    def __init__(self, directory: Path) -> None:
        if not directory.is_absolute():
            raise ValueError("archive directory must be absolute")
        self._directory = directory
        self._directory.mkdir(parents=True, mode=0o700, exist_ok=True)

    def persist(self, document: RepositoryDocument) -> None:
        digest = hashlib.sha256(document.yaml_bytes).hexdigest()
        if not _HEX64.fullmatch(document.yaml_sha256) or digest != document.yaml_sha256:
            raise ConfigHistoryUnavailableError("Configuration YAML SHA256 mismatch")
        if not all((document.config_type, document.config_id, document.version_id)):
            raise ConfigHistoryUnavailableError("configuration document identity is missing")

        manifest = json.dumps(
            [
                document.config_type,
                document.config_id,
                document.version_id,
                document.revision_id,
                document.yaml_sha256,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        name = hashlib.sha256(manifest).hexdigest()
        item_dir = self._directory / name
        item_dir.mkdir(mode=0o700, exist_ok=True)
        self._write_once(item_dir / "document.yaml", document.yaml_bytes)
        self._write_once(item_dir / "manifest.json", manifest)
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

    def load(self, *, config_type: str, config_id: str, version_id: str) -> RepositoryDocument:
        # An archive entry is not trusted merely because it exists on disk.
        for manifest_path in self._directory.glob("*/manifest.json"):
            try:
                fields = json.loads(manifest_path.read_bytes())
                if (
                    not isinstance(fields, list)
                    or len(fields) != 5
                    or fields[:3] != [config_type, config_id, version_id]
                ):
                    continue
                content = (manifest_path.parent / "document.yaml").read_bytes()
                if hashlib.sha256(content).hexdigest() != fields[4]:
                    raise ConfigHistoryUnavailableError("corrupted immutable YAML archive")
                if (
                    hashlib.sha256(
                        json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
                    ).hexdigest()
                    != manifest_path.parent.name
                ):
                    raise ConfigHistoryUnavailableError("corrupted manifest identity")
                return RepositoryDocument(
                    config_type=config_type,
                    config_id=config_id,
                    version_id=version_id,
                    revision_id=int(fields[3]),
                    yaml_bytes=content,
                    yaml_sha256=fields[4],
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ConfigHistoryUnavailableError("malformed historical archive") from exc
        raise ConfigHistoryUnavailableError(
            f"historical configuration unavailable: {config_type}:{config_id}:{version_id}"
        )


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
    ) -> None:
        if not point_table_versions:
            raise ValueError("explicitly pinned point table versions are required")
        self._fetcher = fetcher
        self._archive = archive
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
        self._lock = asyncio.Lock()

    def add_immutable_pins(
        self,
        *,
        point_table_versions: Mapping[str, tuple[str, str]],
        business_points_pin: tuple[str, str] | None,
    ) -> None:
        """Apply additions only; existing immutable interpretation is never replaced."""
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

    async def _load(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> Mapping[str, Any]:
        key = (config_type, config_id, version_id)
        if key not in self._loaded:
            async with self._lock:
                if key not in self._loaded:
                    try:
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

    async def require_exact_versions(self, samples: Sequence[StorageSample]) -> None:
        for sample in samples:
            pin = self._pins.get(sample.point_table_id)
            if not pin:
                raise ConfigHistoryUnavailableError(
                    f"no pinned immutable version for {sample.point_table_id}"
                )
            config_id, version_id = pin
            document = await self._load(
                config_type="point_table",
                config_id=config_id,
                version_id=version_id,
            )
            self._require_table_identity(
                point_table_id=sample.point_table_id,
                config_id=config_id,
                document=document,
            )
            points = document.get("points")
            if not isinstance(points, dict) or sample.point_id not in points:
                raise ConfigHistoryUnavailableError("sample point not defined by pinned PointTable")
            point = points[sample.point_id]
            if not isinstance(point, dict):
                raise ConfigHistoryUnavailableError("source point has invalid semantic definition")
            if sample.data_level == DataLevel.PROTOCOL:
                expected_unit = point.get("unit")
                if not isinstance(expected_unit, str) or sample.unit != expected_unit:
                    raise ConfigHistoryUnavailableError(
                        "PROTOCOL point unit disagrees with pinned PointTable"
                    )
            if sample.data_level == DataLevel.BUSINESS:
                source_business_id = point.get("business_point_id", sample.point_id)
                if source_business_id != sample.business_point_id:
                    # A one-to-many mapping must be explicitly represented by an
                    # immutable mapping closure; no implicit guessing is allowed.
                    raise ConfigHistoryUnavailableError(
                        "business point is not bound by this pinned source point"
                    )
                catalog_pin = self._business_pins.get(sample.point_table_id)
                if catalog_pin is None:
                    raise ConfigHistoryUnavailableError("business point catalog is not pinned")
                business_id, business_version = catalog_pin
                business_document = await self._load(
                    config_type="business_points",
                    config_id=business_id,
                    version_id=business_version,
                )
                business_points = business_document.get("business_points")
                if (
                    not isinstance(business_points, dict)
                    or sample.business_point_id not in business_points
                ):
                    raise ConfigHistoryUnavailableError(
                        "business point missing from pinned catalog"
                    )
                entry = business_points[sample.business_point_id]
                if not isinstance(entry, dict) or entry.get("unit") != sample.unit:
                    raise ConfigHistoryUnavailableError(
                        "business point unit does not match catalog"
                    )

    async def get_unit(
        self,
        *,
        point_table_id: str,
        point_id: str,
        business_point_id: str,
        data_level: DataLevel,
    ) -> str:
        """Resolve original or standardized units from immutable configuration history."""
        if data_level == DataLevel.RAW:
            return ""
        pin = self._pins.get(point_table_id)
        if pin is None:
            raise ConfigHistoryUnavailableError("query PointTable has no pinned version")
        config_id, version_id = pin
        document = await self._load(
            config_type="point_table", config_id=config_id, version_id=version_id
        )
        self._require_table_identity(
            point_table_id=point_table_id, config_id=config_id, document=document
        )
        points = document.get("points")
        if not isinstance(points, dict):
            raise ConfigHistoryUnavailableError("PointTable has no immutable points mapping")
        point = points.get(point_id)
        if not isinstance(point, dict):
            raise ConfigHistoryUnavailableError("historical point was removed or malformed")
        if data_level == DataLevel.PROTOCOL:
            unit = point.get("unit")
            if not isinstance(unit, str) or not unit:
                raise ConfigHistoryUnavailableError("source point unit unavailable")
            return unit
        catalog_pin = self._business_pins.get(point_table_id)
        if catalog_pin is None:
            raise ConfigHistoryUnavailableError("business semantic catalog not pinned")
        if point.get("business_point_id", point_id) != business_point_id:
            raise ConfigHistoryUnavailableError("historical source point mapping disagrees")
        catalog_id, catalog_version = catalog_pin
        catalog = await self._load(
            config_type="business_points",
            config_id=catalog_id,
            version_id=catalog_version,
        )
        entries = catalog.get("business_points")
        if not isinstance(entries, dict):
            raise ConfigHistoryUnavailableError("business catalog malformed")
        definition = entries.get(business_point_id)
        if not isinstance(definition, dict):
            raise ConfigHistoryUnavailableError("business semantic point is unknown")
        unit = definition.get("unit")
        if not isinstance(unit, str) or not unit:
            raise ConfigHistoryUnavailableError("standard business unit unavailable")
        return unit
