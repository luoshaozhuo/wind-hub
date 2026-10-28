"""只读配置诊断 config.py 契约。

- 目录/文件缺失、非法 YAML、跨文件引用错误 → FAIL，且不回显敏感内容。
- 合法 fixture → PASS 及实体计数。
- compare：相同目录 UNCHANGED；改动目录 CHANGED；任一侧无效拒绝比较。
- check/compare 全程只读：配置文件字节哈希不变。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from core.application.diagnostic import DiagnosticStatus
from core.infrastructure.diagnostic.config import check_config, compare_config

FIXTURE = Path("tests/fixtures/configs/example_modbus")
_REQUIRED = (
    "system.yaml",
    "units.yaml",
    "device_models.yaml",
    "devices.yaml",
    "points.yaml",
    "tasks.yaml",
    "sinks.yaml",
    "business_points.yaml",
)


def _hashes(directory: Path) -> dict[str, str]:
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in _REQUIRED}


def test_missing_directory_is_fail(tmp_path: Path) -> None:
    result = check_config(tmp_path / "does-not-exist")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "CONFIG_DIRECTORY_NOT_FOUND"


def test_missing_required_files_reports_locations(tmp_path: Path) -> None:
    result = check_config(tmp_path)

    assert result.status is DiagnosticStatus.FAIL
    codes = {finding.code for finding in result.findings}
    assert codes == {"CONFIG_FILE_MISSING"}
    locations = {Path(finding.location).name for finding in result.findings}
    assert locations == set(_REQUIRED)


def test_valid_fixture_passes_with_counts() -> None:
    result = check_config(FIXTURE)

    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "CONFIG_VALID"
    counts = result.measurements
    assert counts["devices"] >= 1
    assert counts["device_models"] >= 1
    assert counts["point_tables"] >= 1


def test_invalid_yaml_is_fail_without_echoing_content(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    secret = "SUPERSECRET-PASSWORD-VALUE-42"
    (tmp_path / "devices.yaml").write_text(
        f"devices:\n  bad: [unterminated\n  password: {secret}\n", encoding="utf-8"
    )

    result = check_config(tmp_path)

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "CONFIG_INVALID"
    # 只输出异常类型，配置内容（含疑似敏感值）不得出现在报告里。
    assert secret not in result.findings[0].message


def test_cross_reference_error_is_fail(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    devices = (tmp_path / "devices.yaml").read_text(encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        devices.replace("model_id: modbus_wtg", "model_id: no_such_model"),
        encoding="utf-8",
    )

    result = check_config(tmp_path)

    assert result.status is DiagnosticStatus.FAIL


def test_check_is_read_only(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    before = _hashes(tmp_path)

    check_config(tmp_path)

    assert _hashes(tmp_path) == before


def test_compare_identical_directories_reports_unchanged() -> None:
    result = compare_config(FIXTURE, FIXTURE)

    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "CONFIG_UNCHANGED"
    assert result.evidence["has_changes"] is False


def test_compare_changed_directory_reports_sections(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path / "current")
    previous = tmp_path / "previous"
    shutil.copytree(FIXTURE, previous)
    # 在 current 增加一个全新 Sink 引用。
    sinks = tmp_path / "current" / "sinks.yaml"
    sinks.write_text(
        sinks.read_text(encoding="utf-8")
        + "\n  extra_redis:\n    type: redis\n    enabled: false\n    connection:\n"
        "      host: 127.0.0.1\n      port: 6379\n      database: 1\n",
        encoding="utf-8",
    )

    result = compare_config(previous, tmp_path / "current")

    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "CONFIG_CHANGED"
    assert result.evidence["has_changes"] is True


def test_compare_rejects_when_either_side_invalid(tmp_path: Path) -> None:
    result = compare_config(tmp_path / "missing-a", tmp_path / "missing-b")

    assert result.status is DiagnosticStatus.FAIL
    assert all(f.status is DiagnosticStatus.FAIL for f in result.findings)


def test_compare_is_read_only(tmp_path: Path) -> None:
    current = tmp_path / "current"
    shutil.copytree(FIXTURE, current)
    before = _hashes(current)

    compare_config(FIXTURE, current)

    assert _hashes(current) == before


def test_compare_diff_engine_failure_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import core.infrastructure.diagnostic.config as config_diag

    def boom(previous, current):
        raise RuntimeError("diff engine broken")

    monkeypatch.setattr(config_diag, "diff_config_snapshots", boom)

    result = compare_config(FIXTURE, FIXTURE)

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "CONFIG_DIFF_FAILED"
    assert "RuntimeError" in result.findings[0].message
