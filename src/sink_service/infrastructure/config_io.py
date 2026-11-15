"""Sink filesystem, YAML and incoming snapshot codec adapters."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from core.infrastructure.config.generic_yaml import ConfigOperationError, parse_yaml
from sink_service.application.config import (
    _instances,
    template_path,
)
from sink_service.application.config import (
    validate_snapshot as validate_semantics,
)
from sink_service.infrastructure.config_schema import validate_service_technology
from sink_service.domain.config import (
    ConfigurationError,
    SnapshotFile,
    ValidatedSnapshot,
    config_sources,
    expect_mapping,
    expect_nonempty_str,
    require_keys,
    require_version,
    validate_service,
)


def _yaml_doc(item: SnapshotFile) -> dict[str, Any]:
    try:
        decoded = item.content.decode("utf-8", errors="strict")
        raw = parse_yaml(decoded)
    except (UnicodeError, ConfigOperationError) as exc:
        raise ConfigurationError(f"{item.path}: invalid YAML") from exc
    doc = expect_mapping(raw, item.path)
    require_version(doc, item.path)
    if item.path.startswith("configs/templates/sinks/"):
        require_keys(
            doc,
            item.path,
            frozenset(
                {
                    "schema_version",
                    "file_version",
                    "template_id",
                    "kind",
                    "capabilities",
                    "connection",
                    "output",
                }
            ),
        )
    elif item.path.startswith("configs/templates/deliveries/"):
        require_keys(
            doc,
            item.path,
            frozenset(
                {
                    "schema_version",
                    "file_version",
                    "template_id",
                    "delivery_mode",
                    "source_filter",
                }
            ),
            frozenset({"output", "staleness", "reliable"}),
        )
    return doc


def validate_snapshot(files: tuple[SnapshotFile, ...]) -> ValidatedSnapshot:
    docs = {item.path: _yaml_doc(item) for item in files}
    return validate_semantics(files, docs)


def read_snapshot(root: Path, service_path: Path) -> ValidatedSnapshot:
    """Load the canonical Main Service and its referenced configuration closure."""
    root = root.resolve(strict=True)
    selected: list[str] = []
    for path in (service_path,):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ConfigurationError("Service path escapes configuration root")
        relative = resolved.relative_to(root).as_posix()
        if not relative.startswith("configs/services/") or not relative.endswith(".yaml"):
            raise ConfigurationError("Service must be an explicit Service pathname")
        selected.append(relative)
    if selected != ["configs/services/sink_service.yaml"]:
        raise ConfigurationError("canonical Service pathname required")

    def safe_file(pathname: str) -> SnapshotFile:
        target = root / pathname
        if target.is_symlink() or any(
            parent.is_symlink() for parent in target.parents if parent != root
        ):
            raise ConfigurationError(f"symbolic-link configuration path: {pathname}")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ConfigurationError(f"unsafe configuration file: {pathname}")
        return SnapshotFile(pathname, resolved.read_bytes())

    service_paths = tuple(selected)
    files = [safe_file(path) for path in service_paths]
    service = _yaml_doc(files[0])
    validate_service(service)
    validate_service_technology(service)
    instance_paths, template_root = config_sources(service)
    files.extend(safe_file(path) for path in instance_paths.values())
    docs = {item.path: _yaml_doc(item) for item in files}
    required: set[str] = set()
    for family, pathname in instance_paths.items():
        entries = _instances(docs, family, pathname)
        for identifier, value in entries.items():
            name = expect_nonempty_str(
                value.get("template_id"), f"{family}.{identifier}.template_id"
            )
            version = expect_nonempty_str(
                value.get("template_version"), f"{family}.{identifier}.template_version"
            )
            required.add(template_path(template_root, family, name, version))
    files.extend(safe_file(path) for path in sorted(required))
    return validate_snapshot(tuple(files))


def snapshot_from_wire(files: list[tuple[str, bytes, int, str]]) -> ValidatedSnapshot:
    """Validate transport file metadata before parsing any YAML payload."""
    items: list[SnapshotFile] = []
    for path, content, size, expected_hash in files:
        if size != len(content):
            raise ConfigurationError(f"{path}: size_bytes mismatch")
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ConfigurationError(f"{path}: sha256 mismatch")
        items.append(SnapshotFile(path, content))
    return validate_snapshot(tuple(items))


def mutate_delivery(
    item: SnapshotFile, delivery_id: str, *, running: bool, revision: str
) -> SnapshotFile:
    """Serialize one validated operator intent without leaking YAML into Application."""
    doc = _yaml_doc(item)
    deliveries = doc.get("deliveries")
    if not isinstance(deliveries, dict):
        raise ConfigurationError("invalid Deliveries mapping")
    delivery = deliveries.get(delivery_id)
    if not isinstance(delivery, dict):
        raise ConfigurationError("Delivery is missing in current snapshot")
    delivery["desired_state"] = "START" if running else "STOP"
    doc["revision"] = revision
    return SnapshotFile(
        item.path,
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8"),
    )
