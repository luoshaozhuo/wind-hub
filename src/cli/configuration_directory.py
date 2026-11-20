"""Build a bounded Configuration candidate from the complete on-disk configs root."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_VERSION = re.compile(
    r"^(?P<stem>.+)_v(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\.yaml$"
)
_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_TEMPLATE_DIRECTORIES = {
    "point_tables": "point_table",
    "device_models": "device_model",
    "protocols": "protocol",
    "sinks": "sink",
    "deliveries": "delivery",
    "tasks": "task",
}
_INSTANCE_NAMES = {
    "device": "device",
    "devices": "device",
    "site": "site",
    "sites": "site",
    "sink": "sink",
    "sinks": "sink",
    "delivery": "delivery",
    "deliveries": "delivery",
    "task": "task",
    "tasks": "task",
}
_LOCAL_SERVICES = frozenset({"cli.example.yaml"})


@dataclass(frozen=True, slots=True)
class DirectoryDocument:
    config_type: str
    config_id: str
    filename: str
    file_version: tuple[int, int, int]
    content: bytes


def _classify(path: Path, group: str) -> tuple[str, str]:
    """Classify using actual relative layout, not inferred filename prefixes."""
    parts = path.parts
    stem = path.stem
    match = _VERSION.fullmatch(path.name)
    if match is not None:
        stem = match["stem"]
    if group == "instances":
        if len(parts) != 1:
            raise ValueError(f"nested instance configuration is unsupported: {path}")
        prefix = stem.split("_", 1)[0]
        kind = _INSTANCE_NAMES.get(prefix)
        if kind is None:
            raise ValueError(f"unknown instance configuration: {path}")
        # Preserve the existing path-reference identity (devices, devices_sim).
        return kind, stem
    if group == "services":
        if len(parts) != 1:
            raise ValueError(f"nested service configuration is unsupported: {path}")
        name = stem.removeprefix("service_")
        if not name:
            raise ValueError(f"empty service identity: {path}")
        return "service", name
    if group == "templates":
        if len(parts) == 2:
            kind = _TEMPLATE_DIRECTORIES.get(parts[0])
            if kind is None:
                raise ValueError(f"unknown template directory: {path}")
            prefix = kind + "_"
            if not stem.startswith(prefix):
                raise ValueError(f"template filename has incorrect prefix: {path}")
            name = stem[len(prefix) :]
            if not name:
                raise ValueError(f"empty template identity: {path}")
            return kind, name
        if len(parts) == 1:
            for kind in ("business_points", "semantic_points", "observability", "units"):
                if stem == kind:
                    return kind, kind
                if stem.startswith(kind + "_"):
                    return kind, stem[len(kind) + 1 :]
        raise ValueError(f"unknown template configuration: {path}")
    raise ValueError(f"unknown configuration group: {group}")


def _explicit_references(
    document: DirectoryDocument,
) -> set[tuple[str, str, str]]:
    """Extract exact typed version references that must exist in the full tree."""
    payload = yaml.safe_load(document.content)
    references: set[tuple[str, str, str]] = set()

    def add(config_type: object, config_id: object, file_version: object) -> None:
        if (
            not isinstance(config_type, str)
            or not config_type
            or not isinstance(config_id, str)
            or not config_id
            or not isinstance(file_version, str)
            or _SEMVER.fullmatch(file_version) is None
        ):
            raise ValueError(f"invalid typed Configuration reference in {document.filename}")
        references.add((config_type, config_id, file_version))

    sources = payload.get("config_sources", {})
    if isinstance(sources, list):
        for item in sources:
            if not isinstance(item, dict):
                raise ValueError(f"invalid config_sources entry in {document.filename}")
            add(
                item.get("config_type"),
                item.get("config_id"),
                item.get("file_version"),
            )
    if document.config_type == "device_model":
        for field, target_type in (
            ("protocol", "protocol"),
            ("point_table", "point_table"),
            ("task", "task"),
        ):
            target = payload.get(field)
            if target is not None:
                if not isinstance(target, dict):
                    raise ValueError(f"invalid {field} reference in {document.filename}")
                add(target_type, target.get("id"), target.get("version"))
    elif document.config_type == "device":
        devices = payload.get("devices", {})
        if isinstance(devices, dict):
            for entry in devices.values():
                if isinstance(entry, dict) and isinstance(entry.get("model"), dict):
                    model = entry["model"]
                    add("device_model", model.get("id"), model.get("version"))
    elif document.config_type in {"sink", "delivery"}:
        entries_name = "deliveries" if document.config_type == "delivery" else "sinks"
        entries = payload.get(entries_name, {})
        if isinstance(entries, dict):
            for entry in entries.values():
                if isinstance(entry, dict) and "template_id" in entry:
                    add(
                        document.config_type,
                        entry.get("template_id"),
                        entry.get("template_version"),
                    )
    return references


def read_complete_directory(
    root: Path, *, max_bytes: int = 64 * 1024 * 1024, max_files: int = 1000
) -> tuple[DirectoryDocument, ...]:
    """Return every immutable file/version with its root-relative POSIX path."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Configuration root must be a real directory")
    if not 1 <= max_files <= 10000 or max_bytes < 1024:
        raise ValueError("invalid Configuration directory limits")

    available: dict[tuple[str, str, str], DirectoryDocument] = {}
    paths: set[str] = set()
    scanned_bytes = 0
    file_count = 0
    for group in ("instances", "templates", "services"):
        directory = root / group
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"missing or unsafe Configuration directory: {group}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Configuration root contains a symbolic link: {path}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(f"Configuration root contains a special file: {path}")
            if path.suffix == ".yml":
                raise ValueError(f"Configuration YAML must use .yaml extension: {path}")
            if path.suffix != ".yaml":
                raise ValueError(f"unexpected file in Configuration root: {path}")
            file_count += 1
            if file_count > max_files:
                raise ValueError("Configuration root contains too many YAML files")
            # Local bootstraps and examples are not centrally published documents.
            if group == "services" and (
                path.name in _LOCAL_SERVICES
                or path.name.endswith(".example.yaml")
                or path.name.startswith("service_configuration_")
                or path.name.startswith("storage_bootstrap_")
            ):
                continue
            relative = path.relative_to(directory)
            kind, config_id = _classify(relative, group)
            size = path.stat().st_size
            if size < 1 or size > max_bytes - scanned_bytes:
                raise ValueError("Configuration YAML byte budget exhausted")
            content = path.read_bytes()
            scanned_bytes += len(content)
            if len(content) != size:
                raise ValueError(f"Configuration YAML changed during scan: {path}")
            document: Any = yaml.safe_load(content)
            if not isinstance(document, dict) or type(document.get("schema_version")) is not int:
                raise ValueError(f"invalid Configuration schema_version: {path}")
            if document["schema_version"] != 1:
                raise ValueError(f"unsupported Configuration schema_version: {path}")
            declared = document.get("file_version")
            if not isinstance(declared, str) or _SEMVER.fullmatch(declared) is None:
                raise ValueError(f"invalid Configuration file_version: {path}")
            version_parts = declared.split(".")
            version = (
                int(version_parts[0]),
                int(version_parts[1]),
                int(version_parts[2]),
            )
            match = _VERSION.fullmatch(path.name)
            if match is not None and match["version"] != declared:
                raise ValueError(f"Configuration filename/version mismatch: {path}")
            if match is None and re.search(r"_v[0-9]", path.stem):
                raise ValueError(f"malformed Configuration filename version: {path}")
            if (
                kind in {"point_table", "device_model", "protocol", "sink", "delivery", "task"}
                and group == "templates"
            ):
                template_id = document.get("template_id")
                if not isinstance(template_id, str) or not template_id:
                    raise ValueError(f"missing template_id: {path}")
                config_id = template_id
            elif kind == "service":
                service_id = document.get("service_id")
                if service_id is not None:
                    if not isinstance(service_id, str) or not service_id:
                        raise ValueError(f"invalid service_id: {path}")
                    config_id = service_id
            relative_path = path.relative_to(root).as_posix()
            if relative_path in paths:
                raise ValueError(f"duplicate Configuration relative path: {relative_path}")
            paths.add(relative_path)
            selected = DirectoryDocument(kind, config_id, relative_path, version, content)
            version_key = (kind, config_id, declared)
            previous_version = available.get(version_key)
            if previous_version is not None:
                raise ValueError(f"duplicate immutable Configuration version: {path}")
            available[version_key] = selected
    if not available:
        raise ValueError("Configuration root contains no eligible YAML documents")

    for document in available.values():
        for config_type, config_id, file_version in _explicit_references(document):
            if (config_type, config_id, file_version) not in available:
                raise ValueError(
                    f"missing pinned Configuration {config_type}:{config_id}@{file_version}"
                )
    return tuple(available[key] for key in sorted(available))
