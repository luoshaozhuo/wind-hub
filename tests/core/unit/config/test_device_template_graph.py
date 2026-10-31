"""Shared device template dependency resolution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.template_graph import resolve_device_templates
from core.infrastructure.config.versioned_templates import (
    TemplateRef,
    VersionedTemplateRepository,
)


def _template(root: Path, category: str, name: str, body: str) -> None:
    directory = root / category / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "1.0.0.yaml").write_text(
        f"schema_version: 1\ntemplate_id: {name}\nfile_version: 1.0.0\n{body}",
        encoding="utf-8",
    )


def test_device_resolution_does_not_require_collector_task(tmp_path: Path) -> None:
    _template(tmp_path, "protocols", "modbus_tcp", "protocol: modbus\n")
    _template(tmp_path, "point_tables", "turbine", "protocol: modbus\npoints: {}\n")
    _template(
        tmp_path,
        "device_models",
        "turbine",
        "protocol:\n  id: modbus_tcp\n  version: 1.0.0\n"
        "point_table:\n  id: turbine\n  version: 1.0.0\n",
    )
    repository = VersionedTemplateRepository(tmp_path)
    model = repository.resolve(TemplateRef("device_models", "turbine", "1.0.0"))

    result = resolve_device_templates(repository, model)

    assert result.protocol.reference.template_id == "modbus_tcp"
    assert result.point_table.reference.template_id == "turbine"
    assert result.inherited_tables == ()


def test_device_resolution_rejects_protocol_mismatch(tmp_path: Path) -> None:
    _template(tmp_path, "protocols", "modbus_tcp", "protocol: modbus\n")
    _template(tmp_path, "point_tables", "turbine", "protocol: ads\npoints: {}\n")
    _template(
        tmp_path,
        "device_models",
        "turbine",
        "protocol:\n  id: modbus_tcp\n  version: 1.0.0\n"
        "point_table:\n  id: turbine\n  version: 1.0.0\n",
    )
    repository = VersionedTemplateRepository(tmp_path)
    model = repository.resolve(TemplateRef("device_models", "turbine", "1.0.0"))

    with pytest.raises(ConfigError, match="protocol mismatch"):
        resolve_device_templates(repository, model)
