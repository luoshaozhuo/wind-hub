"""Unit tests for ``cli/probe/ports_parse.py`` — 端口规格解析。"""

from __future__ import annotations

import pytest

from wind_hub.adapter.inbound.cli.probe.ports_parse import default_ports, parse_ports
from wind_hub.config.ports_config import PortsConfig, default_ports_config
from wind_hub.domain.model.errors import ConfigError


def test_parse_ports_single() -> None:
    assert parse_ports("502") == [502]


def test_parse_ports_comma_list() -> None:
    assert parse_ports("502,2404,48898") == [502, 2404, 48898]


def test_parse_ports_range() -> None:
    assert parse_ports("8000-8002") == [8000, 8001, 8002]


def test_parse_ports_mixed() -> None:
    assert parse_ports("502,2404,8000-8002") == [502, 2404, 8000, 8001, 8002]


def test_parse_ports_dedupes_preserving_order() -> None:
    assert parse_ports("502,2404,502,502") == [502, 2404]
    assert parse_ports("8000-8002,8001-8003") == [8000, 8001, 8002, 8003]


def test_parse_ports_whitespace_tolerated() -> None:
    assert parse_ports(" 502 , 2404 ") == [502, 2404]


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "abc",
        "0",
        "65536",
        "-1",
        "502,",
        "8000-7000",
        "1-2000",  # 单段范围超 1024 上限
        "8000-",
        "x-8000",
    ],
)
def test_parse_ports_invalid_raises(spec: str) -> None:
    with pytest.raises(ConfigError):
        parse_ports(spec)


def test_parse_ports_total_limit() -> None:
    # 4 段 × 1024 = 4096 内；再加一个端口即超限
    spec = ",".join(f"{base}-{base + 1023}" for base in (1000, 3000, 5000, 7000))
    assert len(parse_ports(spec)) == 4096
    with pytest.raises(ConfigError, match="exceeding the limit"):
        parse_ports(spec + ",9999")


def test_default_ports() -> None:
    """默认端口集 = 内置工业映射的全部端口（mapping.keys()）。"""
    assert default_ports() == [502, 2404, 48898, 4840, 44818, 80, 443, 22, 23]


def test_default_ports_with_custom_config() -> None:
    """config 提供时读 config.mapping 的端口（返回副本，不改原表）。"""
    config = PortsConfig(mapping={502: "modbus", 80: "http", 443: "https"})
    ports = default_ports(config)
    assert ports == [502, 80, 443]
    ports.append(1)
    assert list(config.mapping) == [502, 80, 443]


def test_default_ports_without_config_uses_builtin() -> None:
    """config 缺省回落内置默认端口集（配置文件缺失的兜底）。"""
    assert default_ports(None) == list(default_ports_config().mapping)
