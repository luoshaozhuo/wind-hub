"""Unit tests for ``config/ports_config.py`` — 端口扫描配置加载。

YAML 键为 ``mapping`` / ``timeout`` / ``concurrency``；文件缺失回落内置
工业协议映射；``default_ports()`` 返回 ``mapping`` 的全部端口。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wind_hub.adapter.inbound.cli.probe.ports_parse import default_ports
from wind_hub.config.ports_config import (
    PortsConfig,
    _validate_ports,
    default_ports_config,
    load_ports_config,
)
from wind_hub.domain.model.errors import ConfigError

_YAML = """\
mapping:
  502: modbus
  2404: iec104
  80: http
  443: https
timeout: 2.5
concurrency: 64
"""


def test_load_ports_config_from_yaml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text(_YAML, encoding="utf-8")
    cfg = load_ports_config(cfg_file)

    assert cfg.mapping == {502: "modbus", 2404: "iec104", 80: "http", 443: "https"}
    assert cfg.timeout == 2.5
    assert cfg.concurrency == 64


def test_load_ports_config_missing_file_returns_builtin(tmp_path: Path) -> None:
    """文件不存在 → 内置默认值（向后兼容，不报错）。"""
    cfg = load_ports_config(tmp_path / "nonexistent.yaml")
    assert cfg == default_ports_config()
    assert cfg.mapping[502] == "modbus"
    assert cfg.mapping[48898] == "ads"


def test_load_ports_config_invalid_yaml_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("mapping: {502: :\n  bad", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_ports_config(cfg_file)


def test_load_ports_config_empty_file_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="Empty configuration file"):
        load_ports_config(cfg_file)


def test_load_ports_config_non_mapping_top_level_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("- 502\n- 2404\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_ports_config(cfg_file)


def test_load_ports_config_rejects_empty_mapping(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("mapping: {}", encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty mapping"):
        load_ports_config(cfg_file)


def test_load_ports_config_validates_port_range(tmp_path: Path) -> None:
    for bad in ("mapping: {0: x}", "mapping: {65536: x}", "mapping: {-1: x}"):
        cfg_file = tmp_path / "ports.yaml"
        cfg_file.write_text(bad, encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid port"):
            load_ports_config(cfg_file)


def test_load_ports_config_rejects_non_int_port(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("mapping: {http_port: http}", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid port"):
        load_ports_config(cfg_file)


def test_validate_ports_rejects_duplicates() -> None:
    """端口列表去重校验（mapping 经 YAML dict 无法表达重复键，直接测校验函数）。"""
    with pytest.raises(ConfigError, match="duplicate port 502"):
        _validate_ports([502, 80, 502], "mapping keys", Path("ports.yaml"))


def test_validate_ports_rejects_bool_port() -> None:
    """bool 是 int 子类——必须显式拒绝，不能当合法端口。"""
    with pytest.raises(ConfigError, match="invalid port"):
        _validate_ports([True], "mapping keys", Path("ports.yaml"))


def test_load_ports_config_rejects_empty_service_name(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("mapping: {502: ''}", encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty string"):
        load_ports_config(cfg_file)


def test_load_ports_config_rejects_blank_service_name(tmp_path: Path) -> None:
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("mapping: {502: '   '}", encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty string"):
        load_ports_config(cfg_file)


def test_load_ports_config_validates_timeout(tmp_path: Path) -> None:
    for bad in ("timeout: 0", "timeout: -1.5", "timeout: true"):
        cfg_file = tmp_path / "ports.yaml"
        cfg_file.write_text(bad, encoding="utf-8")
        with pytest.raises(ConfigError, match="timeout must be > 0"):
            load_ports_config(cfg_file)


def test_load_ports_config_validates_concurrency(tmp_path: Path) -> None:
    for bad in ("concurrency: 0", "concurrency: -8", "concurrency: 1.5", "concurrency: false"):
        cfg_file = tmp_path / "ports.yaml"
        cfg_file.write_text(bad, encoding="utf-8")
        with pytest.raises(ConfigError, match="concurrency must be >= 1"):
            load_ports_config(cfg_file)


def test_load_ports_config_partial_file_falls_back_per_field(tmp_path: Path) -> None:
    """只写 mapping 时，timeout / concurrency 取内置默认值。"""
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("mapping: {502: modbus}", encoding="utf-8")
    cfg = load_ports_config(cfg_file)
    assert cfg.mapping == {502: "modbus"}
    assert cfg.timeout == 1.0
    assert cfg.concurrency == 128


def test_load_ports_config_missing_mapping_falls_back_to_builtin(tmp_path: Path) -> None:
    """只写 timeout 时，mapping 回落到内置工业协议映射。"""
    cfg_file = tmp_path / "ports.yaml"
    cfg_file.write_text("timeout: 3.0", encoding="utf-8")
    cfg = load_ports_config(cfg_file)
    assert cfg.mapping == default_ports_config().mapping
    assert cfg.timeout == 3.0


def test_default_ports_config_returns_builtin() -> None:
    cfg = default_ports_config()
    assert isinstance(cfg, PortsConfig)
    assert cfg.mapping == {
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
    assert cfg.timeout == 1.0
    assert cfg.concurrency == 128


def test_default_ports_returns_mapping_keys() -> None:
    """``default_ports(config)`` → ``list(config.mapping)``——未指定 --ports 时的扫描集。"""
    cfg = PortsConfig(mapping={502: "modbus", 2404: "iec104"}, timeout=1.0, concurrency=8)
    assert default_ports(cfg) == [502, 2404]
    assert default_ports(cfg) == list(cfg.mapping.keys())


def test_default_ports_without_config_uses_builtin() -> None:
    assert default_ports() == list(default_ports_config().mapping.keys())


def test_load_ports_config_without_path_returns_builtin() -> None:
    """未提供配置文件路径（未指定 --ports-config）时返回内置默认值。"""
    cfg = load_ports_config()
    assert cfg == default_ports_config()
