"""Unit tests for IEC104 slave proxy reporting config (``reporting.yaml``)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from wind_hub_core.config.reporting import load_reporting
from wind_hub_core.config.schema import ReportingConfig, ReportingPoint
from wind_hub_core.model.errors import ConfigError


def _point(
    device_id: str = "wtg-001",
    point_id: str = "rotor.speed",
    ioa: int = 1001,
    data_type: str = "M_ME_NC_1",
) -> ReportingPoint:
    return ReportingPoint(
        device_id=device_id,
        point_id=point_id,
        ioa=ioa,
        data_type=data_type,
    )


class TestReportingPoint:
    def test_ioa_out_of_range_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ioa"):
            _point(ioa=0x1000000)

    def test_data_type_whitelist_rejected(self) -> None:
        with pytest.raises(ConfigError, match="data_type"):
            _point(data_type="M_BLINK_1")

    def test_valid_point_builds(self) -> None:
        p = _point()
        assert p.ioa == 1001
        assert p.data_type == "M_ME_NC_1"


class TestReportingConfig:
    def test_defaults(self) -> None:
        cfg = ReportingConfig(reporting=[_point()])
        assert cfg.batch_size == 50
        assert cfg.common_address == 1
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 12404

    def test_duplicate_ioa_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ioa"):
            ReportingConfig(reporting=[_point(ioa=1, point_id="a"), _point(ioa=1, point_id="b")])

    def test_duplicate_point_rejected(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate"):
            ReportingConfig(reporting=[_point(ioa=1), _point(ioa=2)])

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="batch_size"):
            ReportingConfig(reporting=[_point()], batch_size=0)


class TestLoadReporting:
    def test_load_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reporting.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "reporting": [
                            {
                                "device_id": "wtg-001",
                                "point_id": "rotor.speed",
                                "ioa": 1001,
                                "data_type": "M_ME_NC_1",
                            }
                        ],
                        "batch_size": 10,
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_reporting(path)
            assert len(cfg.reporting) == 1
            assert cfg.batch_size == 10
            assert cfg.reporting[0].ioa == 1001

    def test_missing_file_raises(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_reporting("/tmp/definitely-missing-reporting.yaml")

    def test_invalid_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reporting.yaml"
            path.write_text("{ bad: [unclosed", encoding="utf-8")
            with pytest.raises(ConfigError, match="Invalid YAML"):
                load_reporting(path)

    def test_invalid_schema_wrapped_as_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reporting.yaml"
            path.write_text(
                yaml.safe_dump({"reporting": [{"device_id": "d", "point_id": "p", "ioa": 1}]}),
                encoding="utf-8",
            )
            with pytest.raises(ConfigError, match="Invalid reporting config"):
                load_reporting(path)
