"""Regression tests for the fail-closed configuration audit."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts.audit_business_points import audit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "configs"
    _write(
        root / "templates/business_points_standard.yaml",
        "schema_version: 1\nfile_version: 1.0.0\nbusiness_points:\n"
        "  active_power:\n    data_type: float32\n    unit: kilowatt\n",
    )
    _write(
        root / "templates/units_standard.yaml",
        "schema_version: 1\nfile_version: 1.0.0\nunits:\n  kilowatt:\n"
        "    quantity: active_power\n",
    )
    _write(
        root / "templates/point_tables/point_table_wtg_v1.0.0.yaml",
        "schema_version: 1\nfile_version: 1.0.0\ntemplate_id: wtg\n"
        "points:\n  p1:\n    business_point_id: active_power\n"
        "    unit: kilowatt\n    data_type: float32\n",
    )
    return root


def test_audit_reports_structurally_valid_point(tmp_path: Path) -> None:
    root = _root(tmp_path)
    output = tmp_path / "results.csv"
    assert audit(root, output) == 0
    with output.open(encoding="utf-8", newline="") as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == 1
    assert records[0]["point_id"] == "p1"
    assert records[0]["issues"] == ""


def test_audit_flags_duplicate_business_identity(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "templates/point_tables/point_table_wtg_v1.0.0.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "  p2:\n    business_point_id: active_power\n"
        "    unit: kilowatt\n    data_type: float32\n",
        encoding="utf-8",
    )
    assert audit(root, tmp_path / "results.csv") == 2


def test_audit_rejects_duplicate_yaml_key(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "templates/point_tables/point_table_wtg_v1.0.0.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "points: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML mapping key"):
        audit(root, tmp_path / "results.csv")


def test_audit_rejects_missing_model_point_table(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(
        root / "templates/device_models/device_model_turbine_v1.0.0.yaml",
        "schema_version: 1\nfile_version: 1.0.0\n"
        "point_table:\n  id: missing\n  version: 1.0.0\n"
        "protocol:\n  id: modbus_tcp\n  version: 1.0.0\n",
    )
    _write(
        root / "templates/protocols/protocol_modbus_tcp_v1.0.0.yaml",
        "schema_version: 1\nfile_version: 1.0.0\n",
    )
    with pytest.raises(ValueError, match="missing point_table target"):
        audit(root, tmp_path / "results.csv")


def test_audit_rejects_missing_device_model(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(
        root / "instances/devices.yaml",
        "schema_version: 1\nfile_version: 1.0.0\n"
        "devices:\n  WT01:\n    model:\n      id: missing\n      version: 1.0.0\n",
    )
    with pytest.raises(ValueError, match="missing model target"):
        audit(root, tmp_path / "results.csv")
