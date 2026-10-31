"""Exact-version dependency traversal for Collector templates."""

from __future__ import annotations

from dataclasses import dataclass

from core.application.errors import ConfigError

from .versioned_templates import ResolvedTemplate, TemplateRef, VersionedTemplateRepository


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


def resolve_model_templates(
    repository: VersionedTemplateRepository,
    model: ResolvedTemplate,
) -> ModelTemplates:
    protocol = repository.resolve(_reference(model.document.get("protocol"), "protocols"))
    point_table = repository.resolve(_reference(model.document.get("point_table"), "point_tables"))
    task = repository.resolve(_reference(model.document.get("task"), "tasks"))
    inherited = validate_point_table_chain(repository, point_table)
    return ModelTemplates(model, protocol, point_table, task, inherited)


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
