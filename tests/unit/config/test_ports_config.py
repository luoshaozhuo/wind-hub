"""Unit tests for ``config/ports_config.py`` — 端口扫描配置加载（step24）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from wind_hub.config.ports_config import (
    PortsConfig,
    default_ports_config,
    load_ports_config,
)
from wind_hub.domain.model.errors import ConfigError

_YAML = """\
default_ports: [502, 2404, 80, 443]
service_map:
  502: modbus
  2404: iec104
  80: http
  443: https
default_timeout: 2.5
default_concurrency: 64
"""


def test_load_ports_config_from_yaml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text(_YAML, encoding="utf-8")
    cfg = load_ports_config(cfg_file)

    assert cfg.default_ports == [502, 2404, 80, 443]
    assert cfg.service_map == {502: "modbus", 2404: "iec104", 80: "http", 443: "https"}
    assert cfg.default_timeout == 2.5
    assert cfg.default_concurrency == 64


def test_load_ports_config_missing_file_returns_builtin(tmp_path: Path) -> None:
    """文件不存在 → 内置默认值（向后兼容，不报错）。"""
    cfg = load_ports_config(tmp_path / "nonexistent.yaml")
    assert cfg == default_ports_config()
    assert cfg.default_ports == [502, 2404, 48898, 4840, 44818]
    assert cfg.service_map[502] == "modbus"


def test_load_ports_config_invalid_yaml_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("default_ports: [502, :\n  bad", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_ports_config(cfg_file)


def test_load_ports_config_validates_port_range(tmp_path: Path) -> None:
    for bad in ("default_ports: [0]", "default_ports: [65536]", "default_ports: [-1]"):
        cfg_file = tmp_path / "ports.yaml"
        cfg_file.write_text(bad, encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid port"):
            load_ports_config(cfg_file)


def test_load_ports_config_rejects_duplicate_ports(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("default_ports: [502, 80, 502]", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate"):
        load_ports_config(cfg_file)


def test_load_ports_config_rejects_bad_service_map_values(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("service_map: {502: ''}", encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty string"):
        load_ports_config(cfg_file)


def test_load_ports_config_partial_file_falls_back_per_field(tmp_path: Path) -> None:
    """只写部分字段时，缺省字段取内置默认值。"""
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("default_ports: [502, 80]", encoding="utf-8")
    cfg = load_ports_config(cfg_file)
    assert cfg.default_ports == [502, 80]
    assert cfg.service_map == default_ports_config().service_map
    assert cfg.default_timeout == 1.0
    assert cfg.default_concurrency == 128


def test_default_ports_config_returns_builtin() -> None:
    cfg = default_ports_config()
    assert isinstance(cfg, PortsConfig)
    assert cfg.default_ports == [502, 2404, 48898, 4840, 44818]
    assert cfg.service_map == {
        502: "modbus",
        2404: "iec104",
        48898: "ads",
        4840: "opc-ua",
        44818: "ethernet-ip",
        80: "http",
        443: "https",
        22: "ssh",
        23: "telnet",
    }
    assert cfg.default_timeout == 1.0
    assert cfg.default_concurrency == 128


def test_load_ports_config_repo_default_file() -> None:
    """仓库自带的 configs/ports.yaml 必须能加载且含关键映射（防配置漂移）。"""
    repo_root = Path(__file__).resolve().parents[3]
    cfg = load_ports_config(repo_root / "configs" / "ports.yaml")
    assert 502 in cfg.default_ports
    assert 80 in cfg.default_ports
    assert cfg.service_map[80] == "http"
    assert cfg.service_map[502] == "modbus"
