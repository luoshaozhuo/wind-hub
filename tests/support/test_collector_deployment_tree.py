"""Shared system fixture must produce deployable, immutable Collector closures."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.validation import (
    resolve_source_paths,
    validate_collector_snapshot,
)
from core.configuration.local_selection import select_local_service
from tests.support.collector_config_tree import (
    SERVICE_NAME,
    build_collector_tree_from_site,
    write_collector_modbus_tree,
)
from tests.support.functional_config import write_functional_config

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def test_modbus_harness_uses_selected_versions(tmp_path: Path) -> None:
    root = write_collector_modbus_tree(
        tmp_path / "modbus", 1502, site_id="farm-a", device_id="WT001"
    )
    service = root / "services" / SERVICE_NAME
    selected = select_local_service(service)
    assert selected.deployment_id == "collector-system"
    assert selected.instances["site"].name == "site_farm-a_v1.0.0.yaml"
    assert selected.instances["devices"].name == "device_system_v1.0.0.yaml"
    assert selected.instances["tasks"].name == "collector_tasks_system_v1.0.0.yaml"
    document = yaml.safe_load(service.read_text(encoding="utf-8"))
    assert set(document["config_sources"]) == {"instances"}
    model = root / "templates/device_models/device_model_modbus_wtg_v1.0.0.yaml"
    assert "task" not in yaml.safe_load(model.read_text(encoding="utf-8"))
    validate_collector_snapshot(resolve_source_paths(service))


def test_flat_system_input_is_published_as_task_instance(tmp_path: Path) -> None:
    flat = write_functional_config(tmp_path / "flat", 1502)
    root = build_collector_tree_from_site(tmp_path / "derived", flat)
    service = root / "services" / SERVICE_NAME
    selected = select_local_service(service)
    tasks = yaml.safe_load(selected.instances["tasks"].read_text(encoding="utf-8"))
    assert tasks["tasks"]
    assert all(item["point_group"] == "all" for item in tasks["tasks"].values())
    assert not (root / "templates/tasks").exists() or not list(
        (root / "templates/tasks").glob("task_*.yaml")
    )
    validate_collector_snapshot(resolve_source_paths(service))
