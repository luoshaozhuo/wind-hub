"""Validate shared configuration examples in the repository."""

from __future__ import annotations

from pathlib import Path

from core.infrastructure.config.document_version import validate_document_version
from core.infrastructure.config.versioned_templates import (
    TemplateRef,
    VersionedTemplateRepository,
)
from core.infrastructure.config.yaml import read_yaml_mapping

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"


def test_shared_instance_and_catalog_versions() -> None:
    for path in (
        _CONFIGS / "instances/site.yaml",
        _CONFIGS / "instances/devices.yaml",
        _CONFIGS / "templates/units.yaml",
        _CONFIGS / "templates/business_points.yaml",
    ):
        metadata = validate_document_version(read_yaml_mapping(path), path)
        assert metadata.file_version == "1.0.0"


def test_versioned_template_examples_resolve() -> None:
    repository = VersionedTemplateRepository(_CONFIGS / "templates")
    references = (
        TemplateRef("device_models", "modbus_wtg", "1.0.0"),
        TemplateRef("protocols", "modbus_tcp", "1.0.0"),
        TemplateRef("point_tables", "modbus_wtg", "1.0.0"),
        TemplateRef("tasks", "turbine_all", "1.0.0"),
    )
    for reference in references:
        resolved = repository.resolve(reference)
        assert resolved.reference == reference
