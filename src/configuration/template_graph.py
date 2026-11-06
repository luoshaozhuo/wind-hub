"""Resolve shared device templates independently of service-specific task templates."""

from __future__ import annotations

from dataclasses import dataclass

from core.application.errors import ConfigError

from .versioned_templates import ResolvedTemplate, TemplateRef, VersionedTemplateRepository


@dataclass(frozen=True, slots=True)
class DeviceTemplates:
    """Service-independent protocol and point-table dependencies."""

    model: ResolvedTemplate
    protocol: ResolvedTemplate
    point_table: ResolvedTemplate
    inherited_tables: tuple[ResolvedTemplate, ...]


@dataclass(frozen=True, slots=True)
class ModelTemplates:
    model: ResolvedTemplate
    protocol: ResolvedTemplate
    point_table: ResolvedTemplate
    task: ResolvedTemplate
    inherited_tables: tuple[ResolvedTemplate, ...]


def _reference(raw: object, category: str) -> TemplateRef:
    if not isinstance(raw, dict) or set(raw) != {"id", "version"}:
        raise ConfigError("template reference must be a mapping")
    return TemplateRef(category, raw["id"], raw["version"])


def resolve_device_templates(
    repository: VersionedTemplateRepository,
    model: ResolvedTemplate,
) -> DeviceTemplates:
    """Resolve only dependencies every device needs, without requiring a task."""
    protocol = repository.resolve(_reference(model.document.get("protocol"), "protocols"))
    point_table = repository.resolve(_reference(model.document.get("point_table"), "point_tables"))
    if point_table.document.get("protocol") != protocol.document.get("protocol"):
        raise ConfigError("device protocol and point table protocol mismatch")
    inherited = validate_point_table_chain(repository, point_table)
    return DeviceTemplates(model, protocol, point_table, inherited)


def resolve_model_templates(
    repository: VersionedTemplateRepository,
    model: ResolvedTemplate,
) -> ModelTemplates:
    device = resolve_device_templates(repository, model)
    task = repository.resolve(_reference(model.document.get("task"), "tasks"))
    return ModelTemplates(
        device.model, device.protocol, device.point_table, task, device.inherited_tables
    )


def validate_point_table_chain(
    repository: VersionedTemplateRepository,
    root: ResolvedTemplate,
) -> tuple[ResolvedTemplate, ...]:
    seen = {root.reference}
    inherited: list[ResolvedTemplate] = []
    current = root
    while current.document.get("parent") is not None:
        reference = _reference(current.document["parent"], "point_tables")
        if reference in seen:
            raise ConfigError("point table inheritance cycle")
        seen.add(reference)
        candidate = repository.resolve(reference)
        if candidate.document.get("protocol") != root.document.get("protocol"):
            raise ConfigError("point table parent protocol mismatch")
        inherited.append(candidate)
        current = candidate
    return tuple(inherited)
