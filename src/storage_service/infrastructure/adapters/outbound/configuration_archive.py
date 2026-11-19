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

import yaml

from storage_service.domain.sample import DataLevel, StorageSample

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ConfigHistoryUnavailable(RuntimeError):
    """An exact immutable configuration version cannot be proved or recovered."""


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
            raise ConfigHistoryUnavailable("Configuration YAML SHA256 mismatch")
        if not all((document.config_type, document.config_id, document.version_id)):
            raise ConfigHistoryUnavailable("configuration document identity is missing")

        manifest = json.dumps(
            [
                document.config_type, document.config_id, document.version_id,
                document.revision_id, document.yaml_sha256,
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
                raise ConfigHistoryUnavailable("content-addressed file was modified")
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
                raise ConfigHistoryUnavailable("archive collision")
        finally:
            if temp_name is not None:
                os.unlink(temp_name)

    def load(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> RepositoryDocument:
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
                    raise ConfigHistoryUnavailable("corrupted immutable YAML archive")
                if hashlib.sha256(
                    json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
                ).hexdigest() != manifest_path.parent.name:
                    raise ConfigHistoryUnavailable("corrupted manifest identity")
                return RepositoryDocument(
                    config_type=config_type, config_id=config_id,
                    version_id=version_id, revision_id=int(fields[3]),
                    yaml_bytes=content, yaml_sha256=fields[4],
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ConfigHistoryUnavailable("malformed historical archive") from exc
        raise ConfigHistoryUnavailable(
            f"historical configuration unavailable: {config_type}:{config_id}:{version_id}"
        )


class ConfigurationPointTableResolver:
    """Fail closed unless exact pinned point/semantic versions validate a sample."""

    def __init__(
        self,
        *, fetcher: DocumentFetcher,
        archive: ImmutableConfigArchive,
        point_table_versions: Mapping[str, str],
        business_points_version: tuple[str, str] | None = None,
    ) -> None:
        if not point_table_versions:
            raise ValueError("explicitly pinned point table versions are required")
        self._fetcher = fetcher
        self._archive = archive
        self._pins = dict(point_table_versions)
        self._business_pin = business_points_version
        self._loaded: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def _load(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> Mapping[str, Any]:
        key = (config_type, config_id, version_id)
        if key not in self._loaded:
            async with self._lock:
                if key not in self._loaded:
                    try:
                        document = await self._fetcher.fetch_by_version(
                            config_type=config_type, config_id=config_id,
                            version_id=version_id,
                        )
                        if (document.config_type, document.config_id, document.version_id) != key:
                            raise ConfigHistoryUnavailable("repository returned wrong version")
                        await asyncio.to_thread(self._archive.persist, document)
                    except (OSError, ConnectionError, TimeoutError):
                        document = await asyncio.to_thread(
                            self._archive.load,
                            config_type=config_type, config_id=config_id,
                            version_id=version_id,
                        )
                    if hashlib.sha256(document.yaml_bytes).hexdigest() != document.yaml_sha256:
                        raise ConfigHistoryUnavailable("YAML digest invalid")
                    parsed: Any = yaml.safe_load(document.yaml_bytes)
                    if not isinstance(parsed, dict):
                        raise ConfigHistoryUnavailable("historical config is not an object")
                    self._loaded[key] = parsed
        return self._loaded[key]

    async def require_exact_versions(self, samples: Sequence[StorageSample]) -> None:
        for sample in samples:
            pin = self._pins.get(sample.point_table_id)
            if not pin:
                raise ConfigHistoryUnavailable(
                    f"no pinned immutable version for {sample.point_table_id}"
                )
            document = await self._load(
                config_type="point_table", config_id=sample.point_table_id,
                version_id=pin,
            )
            points = document.get("points")
            if not isinstance(points, dict) or sample.point_id not in points:
                raise ConfigHistoryUnavailable("sample point not defined by pinned PointTable")
            point = points[sample.point_id]
            if not isinstance(point, dict):
                raise ConfigHistoryUnavailable("source point has invalid semantic definition")
            if sample.data_level == DataLevel.BUSINESS:
                source_business_id = point.get("business_point_id")
                if source_business_id != sample.business_point_id:
                    # A one-to-many mapping must be explicitly represented by an
                    # immutable mapping closure; no implicit guessing is allowed.
                    raise ConfigHistoryUnavailable(
                        "business point is not bound by this pinned source point"
                    )
                if self._business_pin is None:
                    raise ConfigHistoryUnavailable("business point catalog is not pinned")
                business_id, business_version = self._business_pin
                business_document = await self._load(
                    config_type="business_points", config_id=business_id,
                    version_id=business_version,
                )
                business_points = business_document.get("business_points")
                if (
                    not isinstance(business_points, dict)
                    or sample.business_point_id not in business_points
                ):
                    raise ConfigHistoryUnavailable("business point missing from pinned catalog")
                entry = business_points[sample.business_point_id]
                if not isinstance(entry, dict) or entry.get("unit") != sample.unit:
                    raise ConfigHistoryUnavailable("business point unit does not match catalog")
