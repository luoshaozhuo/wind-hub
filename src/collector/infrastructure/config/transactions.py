"""Scoped Collector configuration transactions.

Each mutation validates a full candidate before committing one file atomically.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.errors import ConfigError
from core.infrastructure.config.collector_documents import (
    CollectorDocumentLoader,
    CollectorDocuments,
    ConfigPaths,
)
from core.infrastructure.config.versioned_templates import (
    TemplateRef,
    VersionedTemplateRepository,
    config_hash,
)
from core.infrastructure.config.yaml import read_yaml_mapping, write_yaml_mapping_atomic

from .config_patches import ConfigPatch, apply_patches


class RevisionConflictError(ConfigError):
    """Optimistic configuration revision mismatch."""


class PublishedTemplateExistsError(ConfigError):
    """An immutable template version is already published."""


_SECRET_MARKERS = ("password", "secret", "token", "credential", "private_key", "api_key")


def redact_config(value: Any) -> Any:
    """Recursively hide credentials in configuration responses."""
    if isinstance(value, dict):
        return {
            key: "***"
            if any(marker in key.casefold() for marker in _SECRET_MARKERS)
            else redact_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ConfigSelector:
    scope: str
    name: str
    template_id: str = ""
    template_version: str = ""


@dataclass(frozen=True, slots=True)
class DeviceApplyState:
    device_id: str
    target_revision: str
    applied: bool
    error: str = ""


@dataclass(frozen=True, slots=True)
class ConfigMutationResult:
    stored_revision: str
    active_revision: str
    status: str
    errors: tuple[str, ...] = ()
    devices: tuple[DeviceApplyState, ...] = ()


class CollectorConfigTransactions:
    """Single-document optimistic concurrency, idempotency and audit boundary."""

    def __init__(self, paths: ConfigPaths, documents: CollectorDocuments) -> None:
        self._paths = paths
        self._templates = VersionedTemplateRepository(paths.templates)
        self._lock = asyncio.Lock()
        self._requests: OrderedDict[str, tuple[str, ConfigMutationResult]] = OrderedDict()
        self._statuses: dict[ConfigSelector, ConfigMutationResult] = {}
        self._baseline_selectors: frozenset[ConfigSelector] = frozenset(
            {
                ConfigSelector("SERVICE", "collector"),
                ConfigSelector("INSTANCE", "site"),
                ConfigSelector("INSTANCE", "devices"),
                ConfigSelector("TEMPLATE", "units"),
                ConfigSelector("TEMPLATE", "business_points"),
            }
        )
        self._selectors: set[ConfigSelector] = set()
        self.refresh_references(documents)

    def refresh_references(self, documents: CollectorDocuments) -> None:
        """Maintain an in-memory index for status queries."""
        self._selectors = set(self._baseline_selectors)
        for definition in documents.devices.values():
            for template in (
                definition.model,
                *definition.templates.values(),
                *definition.inherited_tables,
            ):
                reference = template.reference
                self._selectors.add(
                    ConfigSelector(
                        "TEMPLATE",
                        reference.category,
                        reference.template_id,
                        reference.template_version,
                    )
                )

    def refresh_from_disk(self) -> None:
        """Refresh referenced template index after a committed instance change."""
        self.refresh_references(CollectorDocumentLoader(self._paths).load())

    def list_selectors(self) -> tuple[ConfigSelector, ...]:
        return tuple(
            sorted(
                self._selectors | set(self._statuses),
                key=lambda item: (item.scope, item.name, item.template_id, item.template_version),
            )
        )

    def _path(self, selector: ConfigSelector) -> Path:
        if selector.scope == "SERVICE" and selector.name == "collector":
            return self._paths.service.resolve()
        if selector.scope == "INSTANCE" and selector.name in {"site", "devices"}:
            return (self._paths.instances / f"{selector.name}.yaml").resolve()
        if (
            selector.scope == "TEMPLATE"
            and selector.name in {"units", "business_points"}
            and not selector.template_id
            and not selector.template_version
        ):
            return (self._paths.templates / f"{selector.name}.yaml").resolve()
        if selector.scope == "TEMPLATE" and selector.name in {
            "protocols",
            "device_models",
            "point_tables",
            "tasks",
            "deliveries",
        }:
            return self._templates.path_for(
                TemplateRef(selector.name, selector.template_id, selector.template_version)
            )
        raise ConfigError("unsupported configuration selector")

    def read(self, selector: ConfigSelector) -> tuple[dict[str, Any], str]:
        document = read_yaml_mapping(self._path(selector))
        return document, config_hash(document)

    def read_redacted(self, selector: ConfigSelector) -> tuple[dict[str, Any], str]:
        """Return a sanitized document with its actual source hash."""
        raw, revision = self.read(selector)
        return redact_config(raw), revision

    async def replace(
        self,
        selector: ConfigSelector,
        document: dict[str, Any],
        *,
        expected_revision: str,
        request_id: str,
    ) -> ConfigMutationResult:
        if not expected_revision or not request_id:
            raise ConfigError("expected_revision and request_id required")
        path = self._path(selector)
        async with self._lock:
            candidate_hash = config_hash(document)
            request_hash = config_hash(
                {
                    "selector": {
                        "scope": selector.scope,
                        "name": selector.name,
                        "template_id": selector.template_id,
                        "template_version": selector.template_version,
                    },
                    "candidate_hash": candidate_hash,
                    "expected_revision": expected_revision,
                }
            )
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous[0] != request_hash:
                    raise ConfigError("request_id reused for different mutation")
                return self._statuses.get(selector, previous[1])
            exists = path.is_file()
            active_revision = self.status(selector).active_revision if exists else ""
            if exists:
                existing = read_yaml_mapping(path)
                if config_hash(existing) != expected_revision:
                    raise RevisionConflictError("configuration revision conflict")
            elif expected_revision != "CREATE":
                raise ConfigError("new configuration requires CREATE revision")
            if (
                selector.scope == "TEMPLATE"
                and selector.name not in {"units", "business_points"}
                and exists
            ):
                raise PublishedTemplateExistsError("published template versions are immutable")
            self._validate_candidate(selector, deepcopy(document))
            if selector.scope == "TEMPLATE":
                path.parent.mkdir(parents=True, exist_ok=True)
            write_yaml_mapping_atomic(path, document)
            result = ConfigMutationResult(
                stored_revision=candidate_hash,
                active_revision=active_revision,
                status=(
                    "RESTART_REQUIRED"
                    if selector.scope == "SERVICE"
                    or (selector.scope == "INSTANCE" and selector.name == "site")
                    or (
                        selector.scope == "TEMPLATE"
                        and selector.name in {"units", "business_points"}
                    )
                    else "STORED"
                ),
            )
            self._requests[request_id] = (request_hash, result)
            if len(self._requests) > 1024:
                self._requests.popitem(last=False)
            self._statuses[selector] = result
            return result

    async def update(
        self,
        selector: ConfigSelector,
        patches: tuple[ConfigPatch, ...],
        *,
        expected_revision: str,
        request_id: str,
    ) -> ConfigMutationResult:
        """Modify an existing instance or service document."""
        if selector.scope == "TEMPLATE" and selector.name not in {"units", "business_points"}:
            raise ConfigError("published templates cannot be updated")
        document, revision = self.read(selector)
        if revision != expected_revision:
            raise ConfigError("configuration revision conflict")
        updated = apply_patches(document, patches)
        return await self.replace(
            selector,
            updated,
            expected_revision=expected_revision,
            request_id=request_id,
        )

    def _validate_effective_candidate(
        self, selector: ConfigSelector, document: dict[str, Any]
    ) -> None:
        """Resolve all referenced devices against the candidate in memory."""
        from core.infrastructure.config.collector_snapshot import (
            build_collector_snapshot,
        )

        source = CollectorDocumentLoader(
            self._paths,
            candidates={self._path(selector): document},
        )
        build_collector_snapshot(source.load())

    def _validate_template_payload(
        self, selector: ConfigSelector, document: dict[str, Any]
    ) -> None:
        """Check publish-time template structure without modifying the repository."""
        category = selector.name
        if category == "protocols":
            if document.get("protocol") not in {"ads", "modbus", "iec104"}:
                raise ConfigError("protocol template requires a supported protocol")
            return
        if category == "device_models":
            from core.infrastructure.config.versioned_templates import TemplateRef

            for field, kind in (
                ("protocol", "protocols"),
                ("point_table", "point_tables"),
                ("task", "tasks"),
            ):
                ref = document.get(field)
                if not isinstance(ref, dict) or set(ref) != {"id", "version"}:
                    raise ConfigError(f"device model {field} reference is required")
                self._templates.resolve(TemplateRef(kind, ref["id"], ref["version"]))
            return
        if category == "point_tables":
            if document.get("protocol") not in {"ads", "modbus", "iec104"}:
                raise ConfigError("point table protocol is required")
            if not isinstance(document.get("points", {}), dict):
                raise ConfigError("point table points must be a mapping")
            parent = document.get("parent")
            if parent is not None:
                if not isinstance(parent, dict) or set(parent) != {"id", "version"}:
                    raise ConfigError("point table parent reference invalid")
                reference = TemplateRef("point_tables", parent["id"], parent["version"])
                ancestor = self._templates.resolve(reference)
                if ancestor.document.get("protocol") != document["protocol"]:
                    raise ConfigError("point table parent protocol mismatch")
            return
        if category == "tasks":
            interval = document.get("interval")
            if isinstance(interval, bool) or not isinstance(interval, int | float):
                raise ConfigError("task interval must be numeric")
            if interval <= 0 or not isinstance(document.get("point_group"), str):
                raise ConfigError("invalid task interval or point group")
            if document.get("desired_state", "STOP") not in ("START", "STOP"):
                raise ConfigError("invalid default task desired_state")
            return
        if category == "deliveries" and not isinstance(document.get("delivery"), dict):
            raise ConfigError("delivery template requires delivery mapping")

    def _validate_candidate(self, selector: ConfigSelector, document: dict[str, Any]) -> None:
        from core.infrastructure.config.collector_documents import _schema

        _schema(document, self._path(selector))
        if selector.scope == "TEMPLATE":
            if selector.name in {"units", "business_points"}:
                self._validate_effective_candidate(selector, document)
            else:
                if document.get("template_id") != selector.template_id:
                    raise ConfigError("template ID mismatch")
                if str(document.get("template_version")) != selector.template_version:
                    raise ConfigError("template version mismatch")
                self._validate_template_payload(selector, document)
        elif selector.scope == "SERVICE":
            from core.infrastructure.config.collector_service_schema import (
                validate_messaging,
                validate_metrics,
                validate_sections,
                validate_service,
                validate_timeouts,
            )

            validate_service(document)
            validate_sections(document)
            validate_metrics(document)
            validate_messaging(document)
            validate_timeouts(document)
        elif (
            selector.scope == "INSTANCE"
            and selector.name == "devices"
            and not isinstance(document.get("devices"), dict)
        ):
            raise ConfigError("devices must be a mapping")
        if selector.scope in {"SERVICE", "INSTANCE"}:
            self._validate_effective_candidate(selector, document)

    def validate(self, selector: ConfigSelector, document: dict[str, Any]) -> str:
        """Check an in-memory candidate without changing any source file."""
        self._validate_candidate(selector, deepcopy(document))
        return config_hash(document)

    def mark_applied(
        self,
        selector: ConfigSelector,
        revision: str,
        *,
        error: str | None = None,
        devices: tuple[DeviceApplyState, ...] = (),
    ) -> ConfigMutationResult:
        """Record runtime convergence only after a separate apply attempt."""
        stored = self._statuses.get(selector)
        if stored is None or stored.stored_revision != revision:
            raise ConfigError("unknown or superseded stored revision")
        result = ConfigMutationResult(
            stored_revision=revision,
            active_revision=stored.active_revision if error else revision,
            status="APPLY_FAILED" if error else "APPLIED",
            errors=(error,) if error else (),
            devices=devices,
        )
        self._statuses[selector] = result
        return result

    def failed_selectors(self) -> tuple[ConfigSelector, ...]:
        """Return objects requiring retry; service restart requirements are excluded."""
        return tuple(
            selector
            for selector, result in self._statuses.items()
            if result.status in {"APPLY_FAILED", "STORED"}
        )

    def cached_statuses(self) -> tuple[ConfigMutationResult, ...]:
        """Return committed mutation status cache without disk access."""
        return tuple(self._statuses.values())

    def status(self, selector: ConfigSelector) -> ConfigMutationResult:
        """Return last local application status or current on-disk baseline."""
        known = self._statuses.get(selector)
        if known is not None:
            return known
        _, revision = self.read(selector)
        return ConfigMutationResult(revision, revision, "APPLIED")
