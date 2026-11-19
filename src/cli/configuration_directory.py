"""Build an atomic, bounded Configuration candidate from a complete configs/ root."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_VERSION = re.compile(r"^(?P<name>.+)_v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\.yaml$")
_GROUPS: dict[str, dict[str, str]] = {
    "instances": {
        "devices": "device", "device": "device", "sites": "site", "site": "site",
        "sinks": "sink", "sink": "sink", "deliveries": "delivery",
        "delivery": "delivery", "tasks": "task", "task": "task",
    },
    "templates": {
        "point_table": "point_table", "device_model": "device_model",
        "business_points": "business_points", "semantic_points": "semantic_points",
        "units": "units", "protocol": "protocol", "sink": "sink",
        "delivery": "delivery", "task": "task", "observability": "observability",
    },
    "services": {"service": "service"},
}


@dataclass(frozen=True, slots=True)
class DirectoryDocument:
    config_type: str
    config_id: str
    filename: str
    file_version: tuple[int, int, int]
    content: bytes


def read_complete_directory(root: Path, *, max_bytes: int = 64 * 1024 * 1024,
                            max_files: int = 1000) -> tuple[DirectoryDocument, ...]:
    """Select newest declared file_version per identity, rejecting unsafe inputs."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Configuration root must be a real directory")
    if not 1 <= max_files <= 10000 or max_bytes < 1024:
        raise ValueError("invalid Configuration directory limits")
    chosen: dict[tuple[str, str], DirectoryDocument] = {}
    total = 0
    files = 0
    for group in _GROUPS:
        directory = root / group
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"missing or unsafe Configuration directory: {group}")
        for path in sorted(directory.rglob("*.yaml")):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Configuration root contains unsafe YAML: {path}")
            files += 1
            if files > max_files:
                raise ValueError("Configuration root contains too many YAML files")
            # Local bootstrap files and deployment examples are never published.
            if group == "services" and (
                path.name.endswith(".example.yaml")
                or path.name.startswith("service_configuration_")
                or path.name.startswith("storage_bootstrap_")
            ):
                continue
            match = _VERSION.fullmatch(path.name)
            if match is None:
                raise ValueError(f"Configuration filename lacks _vX.Y.Z.yaml: {path.name}")
            name = match["name"]
            categories = _GROUPS[group]
            prefix = next(
                (prefix for prefix in sorted(categories, key=len, reverse=True)
                 if name.startswith(prefix + "_")),
                None,
            )
            if prefix is None:
                raise ValueError(f"unknown Configuration type: {path.name}")
            kind = categories[prefix]
            config_id = name[len(prefix) + 1:]
            if not config_id:
                raise ValueError("Configuration identity suffix is required")
            size = path.stat().st_size
            if size < 1 or size > max_bytes - total:
                raise ValueError("Configuration YAML byte budget exhausted")
            content = path.read_bytes()
            total += len(content)
            if len(content) != size:
                raise ValueError("Configuration YAML changed during directory scan")
            document: Any = yaml.safe_load(content)
            version = tuple(int(item) for item in match["version"].split("."))
            if (
                not isinstance(document, dict)
                or type(document.get("schema_version")) is not int
                or document["schema_version"] != 1
                or document.get("file_version") != match["version"]
            ):
                raise ValueError(f"Configuration YAML header disagrees with filename: {path.name}")
            if kind == "service":
                service_id = document.get("service_id")
                if not isinstance(service_id, str) or not service_id:
                    raise ValueError(f"Service lacks service_id: {path.name}")
                config_id = service_id
            elif kind in {"point_table", "device_model", "protocol"}:
                template_id = document.get("template_id")
                if not isinstance(template_id, str) or not template_id:
                    raise ValueError(f"Template lacks template_id: {path.name}")
                config_id = template_id
            selected = DirectoryDocument(kind, config_id, path.name, version, content)
            identity = (kind, config_id)
            prior = chosen.get(identity)
            if prior is None or selected.file_version > prior.file_version:
                chosen[identity] = selected
            elif selected.file_version == prior.file_version and selected.content != prior.content:
                raise ValueError("same immutable version has conflicting directory content")
    if not chosen:
        raise ValueError("Configuration root contains no eligible YAML documents")
    return tuple(chosen[key] for key in sorted(chosen))
