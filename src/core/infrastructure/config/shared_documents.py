"""Service-independent shared site, device, and template document loading."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.errors import ConfigError

from .business_points import parse_business_points
from .document_version import validate_document_version
from .template_graph import DeviceTemplates, resolve_device_templates
from .units import parse_unit_catalog
from .versioned_templates import (
    TemplateRef,
    VersionedTemplateRepository,
    config_hash,
)
from .yaml import read_yaml_mapping


@dataclass(frozen=True, slots=True)
class SharedConfigPaths:
    instances: Path
    templates: Path


@dataclass(frozen=True, slots=True)
class SharedDeviceDefinition:
    device_id: str
    name: str | None
    templates: DeviceTemplates
    endpoint: Mapping[str, Any]
    protocol_options: Mapping[str, Any]
    enabled: bool


@dataclass(frozen=True, slots=True)
class SharedDocuments:
    site: Mapping[str, Any]
    devices: Mapping[str, SharedDeviceDefinition]
    units: Mapping[str, Any]
    business_points: Mapping[str, Any]
    revision: str


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{context} must be a string-keyed mapping")
    return value


def _keys(document: Mapping[str, Any], keys: frozenset[str], context: str) -> None:
    unknown = document.keys() - keys
    if unknown:
        raise ConfigError(f"{context} unknown fields: {sorted(unknown)}")


def _reference(raw: object, context: str) -> TemplateRef:
    document = _mapping(raw, context)
    _keys(document, frozenset({"id", "version"}), context)
    template_id = document.get("id")
    file_version = document.get("version")
    if not isinstance(template_id, str) or not isinstance(file_version, str):
        raise ConfigError(f"{context}: template reference requires id and version")
    return TemplateRef("device_models", template_id, file_version)


def _read(path: Path) -> dict[str, Any]:
    document = read_yaml_mapping(path)
    validate_document_version(document, path)
    return document


class SharedDocumentLoader:
    """Validate shared documents without reading service configuration."""

    def __init__(self, paths: SharedConfigPaths) -> None:
        self._paths = paths
        self._repository = VersionedTemplateRepository(paths.templates)

    def load(self) -> SharedDocuments:
        site = _read(self._paths.instances / "site.yaml")
        device_doc = _read(self._paths.instances / "devices.yaml")
        units = _read(self._paths.templates / "units.yaml")
        business_points = _read(self._paths.templates / "business_points.yaml")
        _keys(
            site,
            frozenset({"schema_version", "file_version", "site_id", "name"}),
            "site.yaml",
        )
        _keys(
            device_doc,
            frozenset({"schema_version", "file_version", "devices"}),
            "devices.yaml",
        )
        if not isinstance(site.get("site_id"), str) or not site["site_id"].strip():
            raise ConfigError("site.yaml requires non-empty site_id")
        unit_catalog = parse_unit_catalog(
            {
                key: value
                for key, value in units.items()
                if key not in {"schema_version", "file_version"}
            }
        )
        parse_business_points(
            {
                key: value
                for key, value in business_points.items()
                if key not in {"schema_version", "file_version"}
            },
            unit_catalog,
        )
        devices: dict[str, SharedDeviceDefinition] = {}
        for device_id, value in _mapping(device_doc.get("devices"), "devices").items():
            if not device_id.strip() or device_id != device_id.strip():
                raise ConfigError(f"invalid device ID: {device_id!r}")
            raw = _mapping(value, f"device {device_id}")
            _keys(
                raw,
                frozenset({"model", "endpoint", "overrides", "enabled", "name"}),
                f"device {device_id}",
            )
            model = self._repository.resolve(_reference(raw.get("model"), device_id))
            templates = resolve_device_templates(self._repository, model)
            endpoint = _mapping(raw.get("endpoint"), f"device {device_id}.endpoint")
            _keys(endpoint, frozenset({"host", "port"}), f"device {device_id}.endpoint")
            if not isinstance(endpoint.get("host"), str) or not endpoint["host"].strip():
                raise ConfigError(f"device {device_id}: endpoint.host required")
            if "port" in endpoint and (
                type(endpoint["port"]) is not int or not 1 <= endpoint["port"] <= 65535
            ):
                raise ConfigError(f"device {device_id}: invalid endpoint.port")
            overrides = _mapping(raw.get("overrides", {}), f"device {device_id}.overrides")
            options = _mapping(
                overrides.get("protocol_options", {}),
                f"device {device_id}.overrides.protocol_options",
            )
            enabled = raw.get("enabled", True)
            if type(enabled) is not bool:
                raise ConfigError(f"device {device_id}: enabled must be boolean")
            name = raw.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ConfigError(f"device {device_id}: invalid name")
            devices[device_id] = SharedDeviceDefinition(
                device_id=device_id,
                name=name,
                templates=templates,
                endpoint=endpoint,
                protocol_options=options,
                enabled=enabled,
            )
        return SharedDocuments(
            site=site,
            devices=devices,
            units=units,
            business_points=business_points,
            revision=config_hash(
                {
                    "site": site,
                    "devices": device_doc,
                    "units": units,
                    "business_points": business_points,
                    "dependencies": {
                        name: {
                            "model": definition.templates.model.content_hash,
                            "protocol": definition.templates.protocol.content_hash,
                            "point_table": (definition.templates.point_table.content_hash),
                            "parents": [
                                item.content_hash for item in definition.templates.inherited_tables
                            ],
                        }
                        for name, definition in devices.items()
                    },
                }
            ),
        )
