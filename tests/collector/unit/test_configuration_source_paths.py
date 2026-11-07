"""Service-owned source paths: no default directories or indirect references."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.validation import resolve_source_paths
from core.application.errors import ConfigError

pytestmark = pytest.mark.unit


def _service(root: Path, references: object) -> Path:
    services = root / "services"
    services.mkdir(parents=True, exist_ok=True)
    service_file = services / "collector.yaml"
    service_file.write_text(
        yaml.safe_dump({"schema_version": 1, "file_version": "1.0.0", "config_sources": references}),
        encoding="utf-8",
    )
    return service_file


def test_resolves_custom_instance_files_and_template_root(tmp_path: Path) -> None:
    instances = tmp_path / "alternative"
    templates = tmp_path / "custom_templates"
    instances.mkdir()
    templates.mkdir()
    site = instances / "station-a.yaml"
    devices = instances / "turbines-a.yaml"
    site.write_text("site_id: a\n", encoding="utf-8")
    devices.write_text("devices: {}\n", encoding="utf-8")
    config = _service(
        tmp_path,
        {
            "instances": {
                "site": "alternative/station-a.yaml",
                "devices": "alternative/turbines-a.yaml",
            },
            "templates": {"root": "custom_templates"},
        },
    )
    paths = resolve_source_paths(config)
    assert paths.site == site
    assert paths.devices == devices
    assert paths.templates == templates


def test_accepts_absolute_reference_within_config_root(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    templates = tmp_path / "templates"
    instances.mkdir()
    templates.mkdir()
    site = instances / "site.yaml"
    devices = instances / "devices.yaml"
    site.write_text("site_id: a\n", encoding="utf-8")
    devices.write_text("devices: {}\n", encoding="utf-8")
    config = _service(
        tmp_path,
        {
            "instances": {"site": str(site), "devices": str(devices)},
            "templates": {"root": str(templates)},
        },
    )
    paths = resolve_source_paths(config)
    assert paths.site == site.resolve()
    assert paths.templates == templates.resolve()


def test_rejects_absolute_reference_outside_config_root(tmp_path: Path) -> None:
    outside_root = tmp_path / "configs"
    services = outside_root / "services"
    services.mkdir(parents=True)
    (outside_root / "instances").mkdir(parents=True)
    (outside_root / "templates").mkdir()
    foreign = tmp_path / "foreign.yaml"
    foreign.write_text("site_id: a\n", encoding="utf-8")
    (outside_root / "instances" / "devices.yaml").write_text("devices: {}\n", encoding="utf-8")
    config = services / "collector.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "config_sources": {
                    "instances": {
                        "site": str(foreign),
                        "devices": "instances/devices.yaml",
                    },
                    "templates": {"root": "templates"},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="escapes config root"):
        resolve_source_paths(config)


@pytest.mark.parametrize(
    "references",
    [
        {},
        {"instances": {"site": "instances/site.yaml", "devices": "instances/devices.yaml"}},
        {"instances": {"site": "instances/site.yaml"}, "templates": {"root": "templates"}},
        {
            "instances": {"site": "instances/site.yaml", "devices": "instances/devices.yaml"},
            "templates": {"pathname": "templates"},
        },
        {
            "instances": {"site": "../outside.yaml", "devices": "instances/devices.yaml"},
            "templates": {"root": "templates"},
        },
    ],
)
def test_missing_or_indirect_references_rejected(
    tmp_path: Path, references: dict[str, object]
) -> None:
    config = _service(tmp_path, references)
    with pytest.raises(ConfigError):
        resolve_source_paths(config)


def test_no_fallback_to_existing_default_files(tmp_path: Path) -> None:
    (tmp_path / "instances").mkdir()
    (tmp_path / "templates").mkdir()
    (tmp_path / "instances" / "site.yaml").write_text("site_id: a\n", encoding="utf-8")
    (tmp_path / "instances" / "devices.yaml").write_text("devices: {}\n", encoding="utf-8")
    config = _service(
        tmp_path,
        {
            "instances": {"site": "other/site.yaml", "devices": "other/devices.yaml"},
            "templates": {"root": "templates"},
        },
    )
    with pytest.raises(ConfigError, match="missing or invalid"):
        resolve_source_paths(config)


def test_rejects_symlink_source_reference(tmp_path: Path) -> None:
    (tmp_path / "instances").mkdir()
    (tmp_path / "templates").mkdir()
    real = tmp_path / "instances" / "real.yaml"
    real.write_text("site_id: a\n", encoding="utf-8")
    (tmp_path / "instances" / "link.yaml").symlink_to(real)
    (tmp_path / "instances" / "devices.yaml").write_text("devices: {}\n", encoding="utf-8")
    config = _service(
        tmp_path,
        {
            "instances": {"site": "instances/link.yaml", "devices": "instances/devices.yaml"},
            "templates": {"root": "templates"},
        },
    )
    with pytest.raises(ConfigError, match="symlink"):
        resolve_source_paths(config)
