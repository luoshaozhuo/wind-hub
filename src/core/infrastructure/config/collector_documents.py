"""Collector 三类配置的只读候选装配入口。

只处理文件、版本、引用和覆盖契约；不连接设备、不修改磁盘。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError

from .business_points import parse_business_points
from .collector_service_schema import (
    validate_messaging,
    validate_metrics,
    validate_sections,
    validate_service,
    validate_timeouts,
)
from .template_graph import resolve_model_templates
from .units import parse_unit_catalog
from .versioned_templates import (
    ResolvedTemplate,
    TemplateRef,
    VersionedTemplateRepository,
    config_hash,
)
from .yaml import read_yaml_mapping


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    service: Path
    instances: Path
    templates: Path


@dataclass(frozen=True, slots=True)
class DeviceDefinition:
    device_id: str
    name: str | None
    model: ResolvedTemplate
    endpoint: Mapping[str, Any]
    overrides: Mapping[str, Any]
    desired_state: str
    dependency_hashes: Mapping[str, Any]
    enabled: bool
    templates: Mapping[str, ResolvedTemplate]
    inherited_tables: tuple[ResolvedTemplate, ...]


@dataclass(frozen=True, slots=True)
class CollectorDocuments:
    service: Mapping[str, Any]
    site: Mapping[str, Any]
    devices: Mapping[str, DeviceDefinition]
    units: Mapping[str, Any]
    business_points: Mapping[str, Any]
    revision: str


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ConfigError(f"{label} must be a string-keyed mapping")
    return value


def _schema(document: Mapping[str, Any], path: Path) -> None:
    version = document.get("schema_version")
    if type(version) is not int or version != 1:
        raise ConfigError(f"{path}: unsupported schema_version {version!r}")


def _keys(raw: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unexpected = raw.keys() - allowed
    if unexpected:
        raise ConfigError(f"{label}: unknown fields {sorted(unexpected)}")


def _state(value: Any, label: str) -> str:
    if value not in ("START", "STOP") or not isinstance(value, str):
        raise ConfigError(f"{label}: desired_state must be START or STOP")
    return value


class CollectorDocumentLoader:
    """从服务文件、实例根目录和不可变模板根目录生成无副作用候选。"""

    def __init__(
        self,
        paths: ConfigPaths,
        *,
        candidates: Mapping[Path, Mapping[str, Any]] | None = None,
    ) -> None:
        self._candidates = {
            path.resolve(): dict(document) for path, document in (candidates or {}).items()
        }
        self._paths = paths
        self._templates = VersionedTemplateRepository(paths.templates)

    def load_collector(self) -> ConfigSnapshot:
        """Return the fully validated immutable Core domain snapshot."""
        from .collector_snapshot import build_collector_snapshot

        return build_collector_snapshot(self.load())

    def load(self) -> CollectorDocuments:
        service = self._read(self._paths.service)
        _keys(
            service,
            frozenset(
                {"schema_version", "runtime", "watchdog", "messaging", "metrics", "ads", "logs"}
            ),
            "collector.yaml",
        )
        validate_service(service)
        validate_sections(service)
        validate_metrics(service)
        validate_messaging(service)
        validate_timeouts(service)
        site = self._read(self._paths.instances / "site.yaml")
        devices_doc = self._read(self._paths.instances / "devices.yaml")
        _keys(devices_doc, frozenset({"schema_version", "devices"}), "devices.yaml")
        units = self._read(self._paths.templates / "units.yaml")
        business_points = self._read(self._paths.templates / "business_points.yaml")
        unit_catalog = parse_unit_catalog(
            {key: value for key, value in units.items() if key != "schema_version"}
        )
        parse_business_points(
            {key: value for key, value in business_points.items() if key != "schema_version"},
            unit_catalog,
        )
        _keys(site, frozenset({"schema_version", "site_id", "name"}), "site.yaml")
        if not isinstance(site.get("site_id"), str) or not site["site_id"].strip():
            raise ConfigError("site.yaml requires site_id")
        devices: dict[str, DeviceDefinition] = {}
        raw_devices = _mapping(devices_doc.get("devices"), "devices")
        for device_id, raw_value in raw_devices.items():
            raw = _mapping(raw_value, f"device {device_id}")
            _keys(
                raw,
                frozenset({"model", "endpoint", "overrides", "enabled", "name"}),
                f"device {device_id}",
            )
            model_ref = _mapping(raw.get("model"), f"device {device_id}.model")
            _keys(model_ref, frozenset({"id", "version"}), "model reference")
            model_id = model_ref.get("id")
            model_version = model_ref.get("version")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ConfigError(f"device {device_id}: model.id required")
            if not isinstance(model_version, str) or not model_version.strip():
                raise ConfigError(f"device {device_id}: model.version required")
            model = self._templates.resolve(
                TemplateRef(
                    category="device_models",
                    template_id=model_id,
                    template_version=model_version,
                )
            )
            dependencies = resolve_model_templates(self._templates, model)
            overrides = _mapping(raw.get("overrides", {}), "device overrides")
            _keys(
                overrides,
                frozenset({"task", "protocol_options"}),
                f"device {device_id}.overrides",
            )
            _mapping(
                overrides.get("protocol_options", {}),
                f"device {device_id}.protocol_options",
            )
            task_override = _mapping(overrides.get("task", {}), "task overrides")
            _keys(task_override, frozenset({"desired_state"}), "task overrides")
            default_task = _mapping(dependencies.task.document, "task template")
            desired_state = _state(
                task_override.get("desired_state", default_task.get("desired_state", "STOP")),
                f"device {device_id}",
            )
            endpoint = _mapping(raw.get("endpoint"), f"device {device_id}.endpoint")
            _keys(endpoint, frozenset({"host", "port"}), "device endpoint")
            if not isinstance(endpoint.get("host"), str) or not endpoint["host"].strip():
                raise ConfigError(f"device {device_id}: endpoint.host required")
            if "port" in endpoint and (
                type(endpoint["port"]) is not int or not 1 <= endpoint["port"] <= 65535
            ):
                raise ConfigError(f"device {device_id}: invalid endpoint.port")
            if "name" in raw and (not isinstance(raw["name"], str) or not raw["name"].strip()):
                raise ConfigError(f"device {device_id}: name must be non-empty")
            if "enabled" in raw and type(raw["enabled"]) is not bool:
                raise ConfigError(f"device {device_id}: enabled must be boolean")
            devices[device_id] = DeviceDefinition(
                device_id=device_id,
                name=raw.get("name"),
                model=model,
                endpoint=endpoint,
                overrides=overrides,
                desired_state=desired_state,
                enabled=raw.get("enabled", True),
                inherited_tables=dependencies.inherited_tables,
                templates={
                    "protocol": dependencies.protocol,
                    "point_table": dependencies.point_table,
                    "task": dependencies.task,
                },
                dependency_hashes={
                    "protocol": dependencies.protocol.content_hash,
                    "point_table": dependencies.point_table.content_hash,
                    "task": dependencies.task.content_hash,
                    "point_table_parents": [
                        parent.content_hash for parent in dependencies.inherited_tables
                    ],
                },
            )
        return CollectorDocuments(
            service=service,
            site=site,
            devices=devices,
            units=units,
            business_points=business_points,
            revision=config_hash(
                {
                    "service": service,
                    "site": site,
                    "devices": devices_doc,
                    "units": units,
                    "business_points": business_points,
                    "template_hashes": {
                        device_id: {
                            "model": item.model.content_hash,
                            **item.dependency_hashes,
                        }
                        for device_id, item in devices.items()
                    },
                }
            ),
        )

    def _read(self, path: Path) -> dict[str, Any]:
        document = self._candidates.get(path.resolve())
        if document is None:
            document = read_yaml_mapping(path)
        _schema(document, path)
        return document
