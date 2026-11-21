"""Exact Deployment-selected Service/Instance version regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.configuration.local_selection import select_local_service

pytestmark = pytest.mark.unit


def _write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@pytest.fixture
def published_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in ("WIND_HUB_DEPLOYMENT_ID", "WIND_HUB_DEPLOYMENT_VERSION"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "configs"
    (root / "templates").mkdir(parents=True)
    service = root / "services" / "service_gateway_v1.0.0.yaml"
    _write(
        service,
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "service_id": "gateway",
            "config_sources": {"instances": {"devices": "sample"}},
        },
    )
    for version in ("1.0.0", "2.0.0"):
        _write(
            root / "instances" / f"device_sample_v{version}.yaml",
            {"schema_version": 1, "file_version": version, "devices": {}},
        )
        _write(
            root / "deployments" / f"deployment_test_v{version}.yaml",
            {
                "schema_version": 1,
                "file_version": version,
                "deployment_id": "test",
                "services": {"gateway": {"config_id": "gateway", "file_version": "1.0.0"}},
                "instances": {"device": {"sample": {"file_version": version}}},
            },
        )
    return service


def test_ambiguous_deployment_is_not_resolved_by_latest(
    published_tree: Path,
) -> None:
    with pytest.raises(ValueError, match="exactly one Deployment"):
        select_local_service(published_tree)


@pytest.mark.parametrize("version", ["1.0.0", "2.0.0"])
def test_explicit_version_pins_only_the_selected_instance(
    published_tree: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setenv("WIND_HUB_DEPLOYMENT_ID", "test")
    monkeypatch.setenv("WIND_HUB_DEPLOYMENT_VERSION", version)
    selected = select_local_service(published_tree)
    assert selected.deployment_id == "test"
    assert selected.instances["devices"].name == f"device_sample_v{version}.yaml"


@pytest.mark.parametrize("bad", ["latest", "../escape", "01.0.0", ""])
def test_invalid_version_fails_closed(
    published_tree: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("WIND_HUB_DEPLOYMENT_VERSION", bad)
    with pytest.raises(ValueError, match="invalid selected Deployment version"):
        select_local_service(published_tree)


def test_selected_instance_symlink_is_forbidden(
    published_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_DEPLOYMENT_VERSION", "1.0.0")
    path = published_tree.parent.parent / "instances" / "device_sample_v1.0.0.yaml"
    # The test fixture directory is not trusted as a publication source.
    path.unlink()
    path.symlink_to("device_sample_v2.0.0.yaml")
    with pytest.raises(ValueError, match="symbolic-link"):
        select_local_service(published_tree)
