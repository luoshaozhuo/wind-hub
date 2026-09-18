"""Unit tests for configuration loader including cross-file validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from wind_hub.config.loader import (
    load_config,
    load_devices,
    load_points,
    load_routing,
    load_system,
)
from wind_hub.domain.model.errors import ConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(dir_path: Path, name: str, data: dict) -> Path:
    p = dir_path / name
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_load_example_configs(self) -> None:
        """Loading the project's configs/ directory succeeds."""
        cfg = load_config("configs")
        assert cfg.system.scheduler.default_interval == 1.0
        assert len(cfg.system.sinks) == 3
        assert len(cfg.devices.devices) == 3
        assert len(cfg.points.points) == 7
        assert len(cfg.routing.rules) == 3

    def test_load_minimal_valid_config(self) -> None:
        """A minimal valid configuration loads without error."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_yaml(
                base,
                "system.yaml",
                {
                    "sinks": [{"name": "s1", "type": "file"}],
                },
            )
            _write_yaml(
                base,
                "devices.yaml",
                {
                    "devices": [
                        {
                            "device_id": "d1",
                            "protocol": "modbus",
                            "endpoint": {"host": "10.0.0.1", "port": 502},
                        }
                    ],
                },
            )
            _write_yaml(
                base,
                "points.yaml",
                {
                    "points": [
                        {
                            "point_id": "p1",
                            "device_id": "d1",
                            "address": {"type": "holding_register", "register": 30001},
                            "data_type": "float32",
                        }
                    ],
                },
            )
            _write_yaml(
                base,
                "routing.yaml",
                {
                    "rules": [
                        {"name": "default", "targets": ["s1"], "priority": 0},
                    ],
                },
            )
            cfg = load_config(str(base))
            assert len(cfg.devices.devices) == 1


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


class TestMissingFile:
    def test_missing_system_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td, pytest.raises(ConfigError, match="not found"):
            load_config(td)

    def test_missing_devices_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_yaml(base, "system.yaml", {"sinks": []})
            with pytest.raises(ConfigError, match="not found"):
                load_config(str(base))


# ---------------------------------------------------------------------------
# Invalid YAML
# ---------------------------------------------------------------------------


class TestInvalidYaml:
    def test_malformed_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # Write invalid YAML
            base.joinpath("system.yaml").write_text("::: invalid yaml :::")
            with pytest.raises(ConfigError, match="Invalid YAML"):
                load_system(base / "system.yaml")


# ---------------------------------------------------------------------------
# Cross-file validation
# ---------------------------------------------------------------------------


class TestCrossFileValidation:
    def test_point_references_unknown_device_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_yaml(
                base,
                "system.yaml",
                {
                    "sinks": [{"name": "s1", "type": "file"}],
                },
            )
            _write_yaml(
                base,
                "devices.yaml",
                {
                    "devices": [
                        {
                            "device_id": "d1",
                            "protocol": "modbus",
                            "endpoint": {"host": "10.0.0.1", "port": 502},
                        }
                    ],
                },
            )
            _write_yaml(
                base,
                "points.yaml",
                {
                    "points": [
                        {
                            "point_id": "p1",
                            "device_id": "nonexistent",
                            "address": {"type": "hr"},
                            "data_type": "float32",
                        }
                    ],
                },
            )
            _write_yaml(base, "routing.yaml", {"rules": []})
            with pytest.raises(ConfigError, match="unknown device"):
                load_config(str(base))

    def test_point_sink_references_unknown_sink_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_yaml(
                base,
                "system.yaml",
                {
                    "sinks": [{"name": "s1", "type": "file"}],
                },
            )
            _write_yaml(
                base,
                "devices.yaml",
                {
                    "devices": [
                        {
                            "device_id": "d1",
                            "protocol": "modbus",
                            "endpoint": {"host": "10.0.0.1", "port": 502},
                        }
                    ],
                },
            )
            _write_yaml(
                base,
                "points.yaml",
                {
                    "points": [
                        {
                            "point_id": "p1",
                            "device_id": "d1",
                            "address": {"type": "hr"},
                            "data_type": "float32",
                            "sinks": ["ghost_sink"],
                        }
                    ],
                },
            )
            _write_yaml(base, "routing.yaml", {"rules": []})
            with pytest.raises(ConfigError, match="unknown sink"):
                load_config(str(base))

    def test_route_rule_targets_unknown_sink_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_yaml(
                base,
                "system.yaml",
                {
                    "sinks": [{"name": "s1", "type": "file"}],
                },
            )
            _write_yaml(
                base,
                "devices.yaml",
                {
                    "devices": [
                        {
                            "device_id": "d1",
                            "protocol": "modbus",
                            "endpoint": {"host": "10.0.0.1", "port": 502},
                        }
                    ],
                },
            )
            _write_yaml(
                base,
                "points.yaml",
                {
                    "points": [
                        {
                            "point_id": "p1",
                            "device_id": "d1",
                            "address": {"type": "hr"},
                            "data_type": "float32",
                        }
                    ],
                },
            )
            _write_yaml(
                base,
                "routing.yaml",
                {
                    "rules": [
                        {"name": "r1", "targets": ["ghost_sink"], "priority": 0},
                    ],
                },
            )
            with pytest.raises(ConfigError, match="unknown sink"):
                load_config(str(base))


# ---------------------------------------------------------------------------
# Individual loaders
# ---------------------------------------------------------------------------


class TestIndividualLoaders:
    def test_load_system(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "system.yaml",
                {
                    "sinks": [{"name": "s1", "type": "kafka"}],
                },
            )
            cfg = load_system(p)
            assert cfg.sinks[0].name == "s1"

    def test_load_devices(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "devices.yaml",
                {
                    "devices": [
                        {
                            "device_id": "d1",
                            "protocol": "ads",
                            "endpoint": {"host": "10.0.0.1", "port": 851},
                        }
                    ],
                },
            )
            cfg = load_devices(p)
            assert cfg.devices[0].protocol == "ads"

    def test_load_points(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "points.yaml",
                {
                    "points": [
                        {
                            "point_id": "p1",
                            "device_id": "d1",
                            "address": {"type": "hr", "register": 30001},
                            "data_type": "float32",
                        }
                    ],
                },
            )
            cfg = load_points(p)
            assert cfg.points[0].unit is None

    def test_load_routing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "routing.yaml",
                {
                    "rules": [
                        {"name": "default", "targets": ["s1"], "priority": 0},
                    ],
                },
            )
            cfg = load_routing(p)
            assert len(cfg.rules) == 1
