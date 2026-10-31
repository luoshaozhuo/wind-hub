"""Read-only diagnostics for shared instance and template configuration."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from core.application.diagnostic import DiagnosticStatus
from core.infrastructure.diagnostic.config import check_config, compare_config

FIXTURE = Path(__file__).resolve().parents[3] / "configs"
_REQUIRED = (
    "instances/site.yaml",
    "instances/devices.yaml",
    "templates/units.yaml",
    "templates/business_points.yaml",
    "templates/device_models/modbus_wtg/1.0.0.yaml",
    "templates/protocols/modbus_tcp/1.0.0.yaml",
    "templates/point_tables/modbus_wtg/1.0.0.yaml",
)


def _hashes(directory: Path) -> dict[str, str]:
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in _REQUIRED}


def _copy_shared(source: Path, destination: Path) -> None:
    shutil.copytree(source / "instances", destination / "instances")
    shutil.copytree(source / "templates", destination / "templates")


def test_missing_directory_is_fail(tmp_path: Path) -> None:
    result = check_config(tmp_path / "does-not-exist")
    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "CONFIG_DIRECTORY_NOT_FOUND"


def test_missing_required_files_reports_locations(tmp_path: Path) -> None:
    result = check_config(tmp_path)
    assert result.status is DiagnosticStatus.FAIL
    assert {finding.code for finding in result.findings} == {"CONFIG_FILE_MISSING"}
    assert {str(Path(finding.location).relative_to(tmp_path)) for finding in result.findings} == {
        "instances/site.yaml",
        "instances/devices.yaml",
        "templates/units.yaml",
        "templates/business_points.yaml",
    }


def test_valid_fixture_passes_with_counts() -> None:
    result = check_config(FIXTURE)
    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "CONFIG_VALID"
    assert result.measurements["devices"] >= 1
    assert result.measurements["device_models"] >= 1
    assert result.measurements["point_tables"] >= 1
    assert "tasks" not in result.measurements
    assert "sinks" not in result.measurements


def test_invalid_yaml_is_fail_without_echoing_content(tmp_path: Path) -> None:
    _copy_shared(FIXTURE, tmp_path)
    secret = "SUPERSECRET-PASSWORD-VALUE-42"
    (tmp_path / "instances/devices.yaml").write_text(
        f"devices:\n  bad: [unterminated\n  password: {secret}\n",
        encoding="utf-8",
    )
    result = check_config(tmp_path)
    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "CONFIG_INVALID"
    assert secret not in result.findings[0].message


def test_cross_reference_error_is_fail(tmp_path: Path) -> None:
    _copy_shared(FIXTURE, tmp_path)
    devices_path = tmp_path / "instances/devices.yaml"
    text = devices_path.read_text(encoding="utf-8")
    devices_path.write_text(
        text.replace("id: modbus_wtg", "id: no_such_model"),
        encoding="utf-8",
    )
    result = check_config(tmp_path)
    assert result.status is DiagnosticStatus.FAIL


def test_check_is_read_only(tmp_path: Path) -> None:
    _copy_shared(FIXTURE, tmp_path)
    before = _hashes(tmp_path)
    assert check_config(tmp_path).status is DiagnosticStatus.PASS
    assert _hashes(tmp_path) == before


def test_compare_identical_directories_reports_unchanged() -> None:
    result = compare_config(FIXTURE, FIXTURE)
    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "CONFIG_UNCHANGED"
    assert result.evidence["has_changes"] is False


def test_compare_changed_directory_reports_sections(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _copy_shared(FIXTURE, previous)
    _copy_shared(FIXTURE, current)
    site = current / "instances/site.yaml"
    site.write_text(
        site.read_text(encoding="utf-8").replace("Example Wind Farm", "Modified Wind Farm"),
        encoding="utf-8",
    )
    result = compare_config(previous, current)
    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "CONFIG_CHANGED"
    assert result.evidence["has_changes"] is True
    assert result.evidence["site_changed"] is True


def test_compare_rejects_when_either_side_invalid(tmp_path: Path) -> None:
    result = compare_config(tmp_path / "missing-a", tmp_path / "missing-b")
    assert result.status is DiagnosticStatus.FAIL
    assert all(f.status is DiagnosticStatus.FAIL for f in result.findings)


def test_compare_is_read_only(tmp_path: Path) -> None:
    current = tmp_path / "current"
    _copy_shared(FIXTURE, current)
    before = _hashes(current)
    compare_config(FIXTURE, current)
    assert _hashes(current) == before


def test_compare_identical_content_across_directories_has_no_device_changes(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _copy_shared(FIXTURE, previous)
    _copy_shared(FIXTURE, current)

    result = compare_config(previous, current)

    assert result.status is DiagnosticStatus.PASS
    assert result.evidence["has_changes"] is False
    assert result.evidence["sections"]["devices"]["changed"] == []


def test_compare_changed_point_table_marks_dependent_device(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _copy_shared(FIXTURE, previous)
    _copy_shared(FIXTURE, current)
    table = current / "templates/point_tables/modbus_wtg/1.0.0.yaml"
    table.write_text(
        table.read_text(encoding="utf-8").replace("address: 0", "address: 2"),
        encoding="utf-8",
    )

    result = compare_config(previous, current)

    assert result.status is DiagnosticStatus.PASS
    assert result.evidence["has_changes"] is True
    assert result.evidence["sections"]["devices"]["changed"] == ["WT01"]
