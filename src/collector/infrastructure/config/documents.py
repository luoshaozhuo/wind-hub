"""Collector-owned configuration documents composed from the shared Core graph."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.errors import ConfigError
from core.application.settings import LogSettings
from core.infrastructure.config.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
    SharedDocuments,
)
from core.infrastructure.config.template_graph import resolve_model_templates
from core.infrastructure.config.versioned_templates import (
    ResolvedTemplate,
    VersionedTemplateRepository,
    config_hash,
)
from core.infrastructure.config.yaml import read_yaml_mapping


@dataclass(frozen=True, slots=True)
class CollectorPaths:
    service: Path
    instances: Path
    templates: Path


@dataclass(frozen=True, slots=True)
class CollectorDeviceDefinition:
    model: ResolvedTemplate
    templates: Mapping[str, ResolvedTemplate]
    inherited_tables: tuple[ResolvedTemplate, ...]
    endpoint: Mapping[str, Any]
    overrides: Mapping[str, Any]
    desired_state: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class CollectorDocuments:
    service: Mapping[str, Any]
    shared: SharedDocuments
    devices: Mapping[str, CollectorDeviceDefinition]
    revision: str


def _validate_service_sections(service: Mapping[str, Any]) -> None:
    """Reject malformed Collector-only service fields before a config commit."""
    metrics = service.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ConfigError("Collector metrics must be a mapping")
    enabled = metrics.get("enabled", True)
    if type(enabled) is not bool:
        raise ConfigError("Collector metrics.enabled must be boolean")
    windows = metrics.get("windows", [60, 300, 3600])
    if (
        not isinstance(windows, list)
        or not windows
        or any(type(window) is not int or window <= 0 for window in windows)
        or len(set(windows)) != len(windows)
    ):
        raise ConfigError("Collector metrics.windows must contain unique positive integers")
    for key, default in (
        ("pending_capacity", 2048),
        ("event_capacity", 5000),
        ("publish_interval", 60),
    ):
        value = metrics.get(key, default)
        if type(value) is not int or value <= 0:
            raise ConfigError(f"Collector metrics.{key} must be a positive integer")
    logs = service.get("logs", {})
    if not isinstance(logs, dict):
        raise ConfigError("Collector logs must be a mapping")
    LogSettings(**logs)


class CollectorDocumentLoader:
    """Resolve Collector-specific Task/Service data without modifying Core."""

    def __init__(
        self,
        paths: CollectorPaths,
        *,
        candidates: Mapping[Path, Mapping[str, Any]] | None = None,
    ) -> None:
        self._paths = paths
        self._candidates = {
            path.resolve(): dict(document) for path, document in (candidates or {}).items()
        }

    def load(self) -> CollectorDocuments:
        service = self._candidates.get(self._paths.service.resolve())
        if service is None:
            service = read_yaml_mapping(self._paths.service)
        if service.get("schema_version") != 1:
            raise ConfigError("Collector Service schema_version must be 1")
        _validate_service_sections(service)
        device_path = (self._paths.instances / "devices.yaml").resolve()
        raw_devices = self._candidates.get(device_path)
        if raw_devices is None:
            raw_devices = read_yaml_mapping(device_path)
        # Resolve shared entities and Collector-owned task intents from exactly
        # the same device document even during an external atomic replacement.
        documents = {**self._candidates, device_path: raw_devices}
        shared = SharedDocumentLoader(
            SharedConfigPaths(self._paths.instances, self._paths.templates),
            candidates=documents,
        ).load()
        repository = VersionedTemplateRepository(self._paths.templates, candidates=documents)
        raw_instances = raw_devices.get("devices")
        if not isinstance(raw_instances, dict):
            raise ConfigError("Collector devices must be a mapping")
        devices: dict[str, CollectorDeviceDefinition] = {}
        for identifier, definition in shared.devices.items():
            raw = raw_instances[identifier]
            if not isinstance(raw, dict):
                raise ConfigError(f"invalid Collector instance {identifier}")
            overrides = raw.get("overrides", {})
            if not isinstance(overrides, dict):
                raise ConfigError(f"invalid overrides for {identifier}")
            task_overrides = overrides.get("task", {})
            if not isinstance(task_overrides, dict):
                raise ConfigError(f"invalid task overrides for {identifier}")
            templates = resolve_model_templates(repository, definition.templates.model)
            task = templates.task.document
            desired_state = task_overrides.get("desired_state", task.get("desired_state", "STOP"))
            if desired_state not in ("START", "STOP"):
                raise ConfigError(f"invalid desired_state for {identifier}")
            devices[identifier] = CollectorDeviceDefinition(
                model=templates.model,
                templates={
                    "protocol": templates.protocol,
                    "point_table": templates.point_table,
                    "task": templates.task,
                },
                inherited_tables=templates.inherited_tables,
                endpoint=definition.endpoint,
                overrides=overrides,
                desired_state=desired_state,
                enabled=definition.enabled,
            )
        revision = config_hash(
            {
                "service": service,
                "shared_revision": shared.revision,
                "task_templates": {
                    key: value.templates["task"].content_hash for key, value in devices.items()
                },
                "desired_states": {key: value.desired_state for key, value in devices.items()},
            }
        )
        return CollectorDocuments(service, shared, devices, revision)
