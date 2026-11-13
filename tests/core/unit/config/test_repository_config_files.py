"""Validate shared configuration examples in the repository."""

from __future__ import annotations

from pathlib import Path

from core.configuration.document_version import validate_document_version
from core.configuration.versioned_templates import (
    TemplateRef,
    VersionedTemplateRepository,
)
from core.infrastructure.config import read_yaml as read_yaml_mapping

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"


def test_shared_instance_and_catalog_versions() -> None:
    expected_versions = {
        "instances/site.yaml": "1.0.0",
        "instances/devices.yaml": "1.1.0",
        "templates/units_standard.yaml": "1.1.0",
        "templates/business_points_standard.yaml": "1.3.0",
    }
    for name, expected in expected_versions.items():
        path = _CONFIGS / name
        metadata = validate_document_version(read_yaml_mapping(path), path)
        assert metadata.file_version == expected


def test_versioned_template_examples_resolve() -> None:
    repository = VersionedTemplateRepository(_CONFIGS / "templates")
    references = (
        TemplateRef("device_models", "wtg_tz2mw", "1.0.0"),
        TemplateRef("protocols", "modbus_tcp", "1.0.0"),
        TemplateRef("point_tables", "modbus_wtg_tz2mw", "1.0.0"),
        TemplateRef("tasks", "turbine_all", "1.0.0"),
    )
    for reference in references:
        resolved = repository.resolve(reference)
        assert resolved.reference == reference
