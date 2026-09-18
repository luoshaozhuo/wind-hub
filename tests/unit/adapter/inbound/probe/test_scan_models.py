"""Unit tests for ``cli/probe/scan_models.py`` — ScanResult 与网段解析。"""

from __future__ import annotations

import pytest

from wind_hub.adapter.inbound.cli.probe.scan_models import ScanResult, parse_network
from wind_hub.domain.model.errors import ConfigError

# ---------------------------------------------------------------------------
# ScanResult
# ---------------------------------------------------------------------------


def test_scan_result_creation_defaults() -> None:
    result = ScanResult(ip="10.0.1.1", alive=True)
    assert result.mac is None
    assert result.hostname is None
    assert result.detected_by == []


def test_scan_result_to_dict() -> None:
    result = ScanResult(
        ip="10.0.1.1",
        alive=True,
        mac="aa:bb:cc:dd:ee:01",
        hostname="plc-001",
        detected_by=["arp", "icmp"],
    )
    assert result.to_dict() == {
        "ip": "10.0.1.1",
        "alive": True,
        "mac": "aa:bb:cc:dd:ee:01",
        "hostname": "plc-001",
        "detected_by": ["arp", "icmp"],
    }


# ---------------------------------------------------------------------------
# parse_network — CIDR
# ---------------------------------------------------------------------------


def test_parse_network_cidr_30_excludes_network_and_broadcast() -> None:
    assert parse_network("10.0.1.0/30") == ["10.0.1.1", "10.0.1.2"]


def test_parse_network_cidr_32_keeps_single_host() -> None:
    assert parse_network("127.0.0.1/32") == ["127.0.0.1"]


def test_parse_network_bare_ip_treated_as_32() -> None:
    assert parse_network("10.0.1.5") == ["10.0.1.5"]


def test_parse_network_cidr_host_bits_set_is_normalized() -> None:
    # strict=False：10.0.1.7/30 按 10.0.1.4/30 展开
    assert parse_network("10.0.1.7/30") == ["10.0.1.5", "10.0.1.6"]


# ---------------------------------------------------------------------------
# parse_network — 范围
# ---------------------------------------------------------------------------


def test_parse_network_range() -> None:
    assert parse_network("10.0.1.1-10.0.1.4") == [
        "10.0.1.1",
        "10.0.1.2",
        "10.0.1.3",
        "10.0.1.4",
    ]


def test_parse_network_range_single_address() -> None:
    assert parse_network("10.0.1.9-10.0.1.9") == ["10.0.1.9"]


# ---------------------------------------------------------------------------
# parse_network — 错误与上限
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "network",
    ["", "abc", "10.0.1.0/33", "10.0.1.1-10.0.1.0", "10.0.1.x-10.0.1.9", "10.0.1.1-abc"],
)
def test_parse_network_invalid_raises(network: str) -> None:
    with pytest.raises(ConfigError):
        parse_network(network)


def test_parse_network_exceeds_limit_raises() -> None:
    with pytest.raises(ConfigError, match="exceeding the limit"):
        parse_network("10.0.0.0/16")  # 65534 > 4096


def test_parse_network_limit_is_configurable() -> None:
    ips = parse_network("10.0.1.0/24", max_ips=300)
    assert len(ips) == 254
    with pytest.raises(ConfigError):
        parse_network("10.0.1.0/24", max_ips=100)
