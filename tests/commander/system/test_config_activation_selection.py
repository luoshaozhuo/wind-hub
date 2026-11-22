"""Commander Service/Instance selection regression tests.

These tests exercise the current local Commander application boundary.
Repository-to-Commander Observer process-level acceptance remains separate.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from commander.application.config_manager import CommanderConfigManager
from commander.infrastructure.config.service_settings import load_service_settings
from tests.commander.system.test_b1_effective_options import (
    _build_tree,
    _devices_yaml,
    _model_yaml,
)

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _read(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _build_manager(service_path: list[Path]) -> CommanderConfigManager:
    async def quiesce(_devices: frozenset[str]) -> frozenset[str]:
        return frozenset()

    manager = CommanderConfigManager(
        quiesce=quiesce,
        settings_loader=lambda: load_service_settings(service_path[0]),
    )
    manager.start()
    return manager


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    return _build_tree(
        tmp_path,
        devices_yaml=_devices_yaml(port=1502, word_order=None),
        model_yaml=_model_yaml(port=1502),
    )


def _select_new_service(root: Path, original: Path, changes: dict) -> Path:
    next_service = original.with_name("service_commander_v1.0.1.yaml")
    document = _read(original)
    document["file_version"] = "1.0.1"
    for section, values in changes.items():
        document.setdefault(section, {}).update(values)
    _write(next_service, document)

    old_manifest = root / "deployments/deployment_b1-test_v1.0.0.yaml"
    next_manifest = root / "deployments/deployment_b1-test_v1.0.1.yaml"
    manifest = deepcopy(_read(old_manifest))
    manifest["file_version"] = "1.0.1"
    manifest["services"]["commander"]["file_version"] = "1.0.1"
    _write(next_manifest, manifest)
    return next_service


async def test_new_service_write_timeout_requires_restart_and_bootstraps_new_value(
    tmp_path: Path,
) -> None:
    root, service = _tree(tmp_path)
    selection = [service]
    manager = _build_manager(selection)
    before = manager.active_settings.write_timeout
    fingerprint = manager.device_revision("WT-B1")
    next_service = _select_new_service(root, service, {"timeouts": {"write_default_ms": 2500}})
    selection[0] = next_service

    report = await manager.reload()
    assert report.restart_required
    assert report.changed == ()
    assert manager.active_settings.write_timeout == before
    assert manager.device_revision("WT-B1") == fingerprint
    assert _read(service)["file_version"] == "1.0.0"

    restarted = _build_manager(selection)
    assert restarted.active_settings.write_timeout == pytest.approx(2.5)
    assert restarted.device_revision("WT-B1") == fingerprint


async def test_missing_tls_paths_remain_restart_only_and_cannot_replace_active_settings(
    tmp_path: Path,
) -> None:
    root, service = _tree(tmp_path)
    selection = [service]
    manager = _build_manager(selection)
    original_revision = manager.revision
    original_settings = manager.active_settings
    next_service = _select_new_service(
        root,
        service,
        {
            "grpc": {
                "listen_host": "127.0.0.1",
                "listen_port": 50052,
                "max_active_rpcs": 16,
            },
            "security": {
                "ca_cert": str(tmp_path / "missing-ca.crt"),
                "server_cert": str(tmp_path / "missing-server.crt"),
                "server_key": str(tmp_path / "missing-server.key"),
                "callers": [
                    {
                        "caller_id": "ops",
                        "certificate_identity": "spiffe://wind-hub/ops",
                        "permissions": ["query", "control"],
                    }
                ],
            },
        },
    )
    selection[0] = next_service
    report = await manager.reload()

    assert report.restart_required
    assert report.changed == ()
    assert manager.revision == original_revision
    assert manager.active_settings == original_settings
    assert not (tmp_path / "missing-server.key").exists()


async def test_unselected_new_template_does_not_rotate_device_fingerprint(
    tmp_path: Path,
) -> None:
    root, service = _tree(tmp_path)
    manager = _build_manager([service])
    original_revision = manager.revision
    fingerprint = manager.device_revision("WT-B1")
    new_model = root / "templates/device_models/device_model_modbus_wtg_v1.0.1.yaml"
    document = _read(root / "templates/device_models/device_model_modbus_wtg_v1.0.0.yaml")
    document["file_version"] = "1.0.1"
    document["protocol_options"]["slave_id"] = 2
    _write(new_model, document)

    report = await manager.reload()
    assert not report.restart_required
    assert report.changed == ()
    assert report.blocked == ()
    assert manager.revision == original_revision
    assert manager.device_revision("WT-B1") == fingerprint
