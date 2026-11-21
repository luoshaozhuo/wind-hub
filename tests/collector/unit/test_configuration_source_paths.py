"""Collector resolves only exact Deployment-selected logical Instances."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.validation import resolve_source_paths
from core.application.errors import ConfigError

pytestmark = pytest.mark.unit


def _dump(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _tree(root: Path) -> Path:
    configs = root / "configs"
    service = configs / "services" / "service_collector-realtime_v1.0.0.yaml"
    _dump(
        service,
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "service_id": "collector-realtime",
            "config_sources": {
                "instances": {
                    "site": "example_site",
                    "devices": "farm-a",
                    "tasks": "farm-a",
                }
            },
        },
    )
    for filename, field in (
        ("site_example_site_v1.0.0.yaml", "site_id"),
        ("device_farm-a_v1.0.0.yaml", "devices"),
        ("collector_tasks_farm-a_v1.0.0.yaml", "tasks"),
    ):
        _dump(
            configs / "instances" / filename,
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                field: "example_site" if field == "site_id" else {},
            },
        )
    (configs / "templates").mkdir()
    _dump(
        configs / "deployments" / "deployment_test_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "deployment_id": "test",
            "services": {
                "collector-realtime": {
                    "config_id": "collector-realtime",
                    "file_version": "1.0.0",
                }
            },
            "instances": {
                "site": {"example_site": {"file_version": "1.0.0"}},
                "device": {"farm-a": {"file_version": "1.0.0"}},
                "collector_tasks": {"farm-a": {"file_version": "1.0.0"}},
            },
        },
    )
    return service


def test_resolves_exact_selected_instance_files(tmp_path: Path) -> None:
    service = _tree(tmp_path)
    selected = resolve_source_paths(service)
    configs = tmp_path / "configs"
    assert selected.site == configs / "instances/site_example_site_v1.0.0.yaml"
    assert selected.devices == configs / "instances/device_farm-a_v1.0.0.yaml"
    assert selected.tasks == configs / "instances/collector_tasks_farm-a_v1.0.0.yaml"
    assert selected.templates == configs / "templates"


def test_missing_selected_instance_fails_closed(tmp_path: Path) -> None:
    service = _tree(tmp_path)
    (tmp_path / "configs/instances/device_farm-a_v1.0.0.yaml").unlink()
    with pytest.raises(ConfigError, match="Deployment selection failed"):
        resolve_source_paths(service)


def test_deployment_version_does_not_guess_latest(tmp_path: Path) -> None:
    service = _tree(tmp_path)
    manifest = tmp_path / "configs/deployments/deployment_test_v1.0.0.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["instances"]["device"]["farm-a"]["file_version"] = "2.0.0"
    _dump(manifest, data)
    with pytest.raises(ConfigError, match="Deployment selection failed"):
        resolve_source_paths(service)


@pytest.mark.parametrize(
    "value",
    ("instances/device_farm-a_v1.0.0.yaml", "../escape", "", "/absolute"),
)
def test_path_references_are_not_logical_instance_ids(
    tmp_path: Path, value: str
) -> None:
    service = _tree(tmp_path)
    doc = yaml.safe_load(service.read_text(encoding="utf-8"))
    doc["config_sources"]["instances"]["devices"] = value
    _dump(service, doc)
    with pytest.raises(ConfigError, match="Deployment selection failed"):
        resolve_source_paths(service)


def test_service_must_not_reference_templates_directly(tmp_path: Path) -> None:
    service = _tree(tmp_path)
    doc = yaml.safe_load(service.read_text(encoding="utf-8"))
    doc["config_sources"]["templates"] = {"root": "configs/templates"}
    _dump(service, doc)
    with pytest.raises(ConfigError, match="Deployment selection failed"):
        resolve_source_paths(service)


def test_symlinked_selected_instance_is_rejected(tmp_path: Path) -> None:
    service = _tree(tmp_path)
    selected = tmp_path / "configs/instances/device_farm-a_v1.0.0.yaml"
    selected.unlink()
    selected.symlink_to("collector_tasks_farm-a_v1.0.0.yaml")
    with pytest.raises(ConfigError, match="Deployment selection failed"):
        resolve_source_paths(service)
