"""Immutable directory-manifest contract for full configuration publication."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class DirectoryFile:
    relative_path: str
    yaml_bytes: bytes

    def __post_init__(self) -> None:
        path = self.relative_path
        if (
            not isinstance(path, str)
            or not path
            or len(path.encode("utf-8")) > 512
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or PurePosixPath(path).as_posix() != path
            or not path.endswith(".yaml")
        ):
            raise ValueError("invalid configuration directory relative_path")
        if not isinstance(self.yaml_bytes, bytes) or not self.yaml_bytes:
            raise ValueError("configuration YAML must be nonempty bytes")
        try:
            self.yaml_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("configuration YAML must be UTF-8") from exc


@dataclass(frozen=True, slots=True)
class DirectoryManifest:
    files: tuple[DirectoryFile, ...]
    sha256: str
    total_bytes: int


def build_directory_manifest(
    documents: tuple[DirectoryFile, ...],
    *,
    max_documents: int,
    max_bytes: int,
) -> DirectoryManifest:
    """Validate and hash an entire sorted publication snapshot.

    A manifest is a complete directory view, never a patch or a file-list delta.
    The digest binds path names and byte content; repeated requests are stable
    regardless of client input order. The caller decides bootstrap exclusions.
    """
    if max_documents < 1 or max_bytes < 1:
        raise ValueError("publication limits must be positive")
    if not documents:
        raise ValueError("empty full-directory publication is forbidden")
    if len(documents) > max_documents:
        raise ValueError("directory publication exceeds document count limit")
    if len({item.relative_path.casefold() for item in documents}) != len(documents):
        raise ValueError("duplicate configuration relative_path")
    ordered = tuple(sorted(documents, key=lambda item: item.relative_path))
    total = sum(len(item.yaml_bytes) for item in ordered)
    if total > max_bytes:
        raise ValueError("directory publication exceeds byte limit")
    payload = [
        {
            "relative_path": item.relative_path,
            "yaml_sha256": hashlib.sha256(item.yaml_bytes).hexdigest(),
        }
        for item in ordered
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return DirectoryManifest(ordered, hashlib.sha256(encoded).hexdigest(), total)


@dataclass(frozen=True, slots=True)
class PublicationIdentity:
    config_type: str
    config_id: str
    file_version: str


_TEMPLATE_TYPES = {
    "point_tables": ("point_table", "point_table_"),
    "device_models": ("device_model", "device_model_"),
    "protocols": ("protocol", "protocol_"),
    "tasks": ("task", "task_"),
    "sinks": ("sink", "sink_"),
    "deliveries": ("delivery", "delivery_"),
}
_INSTANCE_TYPES = {
    "devices": "device",
    "devices_sim": "device",
    "site": "site",
    "sinks": "sink",
    "deliveries": "delivery",
}
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def resolve_publication_identity(
    relative_path: str,
    document: dict[str, object],
) -> PublicationIdentity:
    """Map an explicitly supported configuration path to its stable identity."""
    DirectoryFile(relative_path, b"identity-check")
    version = document.get("file_version")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ValueError(f"invalid file_version: {relative_path}")
    parts = relative_path.split("/")
    if len(parts) == 3 and parts[0] == "templates":
        category = _TEMPLATE_TYPES.get(parts[1])
        if category is None:
            raise ValueError(f"unsupported template directory: {relative_path}")
        config_type, prefix = category
        name = parts[2]
        marker = f"_v{version}.yaml"
        if not name.startswith(prefix) or not name.endswith(marker):
            raise ValueError(f"template path/version mismatch: {relative_path}")
        logical_id = name[len(prefix) : -len(marker)]
        if not logical_id or document.get("template_id") != logical_id:
            raise ValueError(f"template path/id mismatch: {relative_path}")
        return PublicationIdentity(config_type, logical_id, version)
    if len(parts) == 2 and parts[0] == "instances":
        name = parts[1].removesuffix(".yaml")
        config_type = _INSTANCE_TYPES.get(name)
        if config_type is None:
            raise ValueError(f"unsupported instance file: {relative_path}")
        return PublicationIdentity(config_type, name, version)
    if len(parts) == 2 and parts[0] == "services":
        name = parts[1].removesuffix(".yaml")
        if name.startswith("service_configuration_v"):
            raise ValueError("Configuration bootstrap must be outside publication root")
        return PublicationIdentity("service", name, version)
    if relative_path == "templates/business_points_standard.yaml":
        return PublicationIdentity("business_points", "standard", version)
    if relative_path == "templates/units_standard.yaml":
        return PublicationIdentity("units", "standard", version)
    if relative_path == f"templates/observability_v{version}.yaml":
        return PublicationIdentity("observability", "observability", version)
    raise ValueError(f"unsupported configuration file path: {relative_path}")
