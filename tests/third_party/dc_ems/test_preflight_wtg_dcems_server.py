"""Preflight must distinguish missing release artifacts from executed T02 evidence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tests.third_party.dc_ems.preflight_wtg_dcems_server import (
    _release_version,
    inspect,
)


def test_preflight_reports_missing_release_assets_without_secrets(tmp_path: Path) -> None:
    """Never claim T02 readiness from a standalone test checkout."""
    release = tmp_path / "release"
    release.mkdir()
    (release / "VERSION").write_text("release_version=1.1.4-583d5da\n", encoding="utf-8")
    with patch(
        "tests.third_party.dc_ems.preflight_wtg_dcems_server._image_exists",
        return_value=False,
    ):
        report = inspect(tmp_path / "run", release)
    assert not report["ready_for_start_attempt"]
    assert not report["ready_for_T02_acceptance"]
    assert report["checks"]["database:business_schema"] == "UNVERIFIED"
    assert report["checks"]["server:actual_modbus_fc16"] == "UNVERIFIED"
    assert report["checks"]["license:configured"] == "MISSING"
    assert len([key for key in report["checks"] if key.startswith("image:")]) == 2


def test_preflight_does_not_treat_complete_files_as_executed_t02(
    tmp_path: Path,
) -> None:
    """Prepared files permit a start attempt, never an automatic acceptance."""
    release = tmp_path / "release"
    release.mkdir()
    (release / "VERSION").write_text("release_version=1.1.4-583d5da\n", encoding="utf-8")
    root = tmp_path / "run"
    for relative in (
        "data.json",
        "docker/config/db.property",
        "docker/config/ssl/dc-ems.jks",
        "docker/config/log4j.properties",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    (root / "docker/dcems.env").write_text("LICENSE_CODE=secret-test-fixture\n", encoding="utf-8")
    with patch(
        "tests.third_party.dc_ems.preflight_wtg_dcems_server._image_exists",
        return_value=True,
    ):
        report = inspect(root, release)
    assert report["ready_for_start_attempt"] is True
    assert report["ready_for_T02_acceptance"] is False
    assert report["checks"]["database:business_schema"] == "UNVERIFIED"
    assert "secret-test-fixture" not in str(report)


@pytest.mark.parametrize("invalid", ["", "../evil", "test:latest", "test name"])
def test_preflight_rejects_unsafe_version(tmp_path: Path, invalid: str) -> None:
    """A release VERSION cannot inject Docker image references."""
    path = tmp_path / "VERSION"
    path.write_text(f"release_version={invalid}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _release_version(path)


def test_preflight_reports_missing_release_version_without_crashing(tmp_path: Path) -> None:
    """Missing release VERSION is a failed prerequisite, not an unhandled traceback."""
    report = inspect(tmp_path / "run", tmp_path / "release")
    assert report["checks"] == {"release:version": "MISSING"}
    assert report["missing"] == ["release:version"]
    assert not report["ready_for_start_attempt"]
    assert not report["ready_for_T02_acceptance"]


def test_preflight_reports_invalid_release_version(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "VERSION").write_text("release_version=../evil\n", encoding="utf-8")
    report = inspect(tmp_path / "run", release)
    assert report["missing"] == ["release:version"]
    assert not report["ready_for_start_attempt"]
