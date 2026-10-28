"""三种协议的配置解析单元测试。"""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.ads.config import is_valid_ams_net_id, parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config


def test_ads_default_runtime_port_and_override() -> None:
    endpoint = ConnectionEndpoint("192.0.2.10")
    assert parse_ads_config(endpoint, {}).target_port == 801
    assert parse_ads_config(endpoint, {"twincat_version": "3"}).target_port == 851
    assert parse_ads_config(ConnectionEndpoint("192.0.2.10", 852), {}).target_port == 852


@pytest.mark.parametrize("net_id", ["1.2.3.4.5.6", "255.255.255.255.255.255"])
def test_ads_valid_net_ids(net_id: str) -> None:
    assert is_valid_ams_net_id(net_id)


@pytest.mark.parametrize("net_id", ["1.2.3", "1.2.3.4.5.256", "a.b.c.d.e.f"])
def test_ads_invalid_net_ids(net_id: str) -> None:
    assert not is_valid_ams_net_id(net_id)


def test_ads_invalid_route_option() -> None:
    with pytest.raises(ConfigError, match="route_auto_add"):
        parse_ads_config(ConnectionEndpoint("192.0.2.10"), {"route_auto_add": "true"})


def test_iec104_windows_and_timeouts() -> None:
    config = parse_iec104_config(
        ConnectionEndpoint("192.0.2.11"),
        {"k": 16, "w": 8, "read_timeout": 1.5},
    )
    assert (config.port, config.k, config.w, config.read_timeout) == (2404, 16, 8, 1.5)


@pytest.mark.parametrize("options", [{"k": 2, "w": 3}, {"t1": 1.0, "t2": 2.0}])
def test_iec104_invalid_windows(options: dict[str, float]) -> None:
    with pytest.raises(ConfigError):
        parse_iec104_config(ConnectionEndpoint("192.0.2.11"), options)


def test_modbus_endpoint_and_slave_id() -> None:
    config = parse_modbus_config(
        ConnectionEndpoint("192.0.2.12", 1502),
        {"slave_id": 12, "word_order": "big_endian"},
    )
    assert (config.port, config.slave_id, config.word_order) == (1502, 12, "big_endian")


@pytest.mark.parametrize("slave_id", [-1, 256, True, "1"])
def test_modbus_invalid_slave_id(slave_id: object) -> None:
    with pytest.raises(ConfigError):
        parse_modbus_config(ConnectionEndpoint("192.0.2.12"), {"slave_id": slave_id})


def test_modbus_rejects_non_tcp() -> None:
    with pytest.raises(ConfigError, match="mode"):
        parse_modbus_config(ConnectionEndpoint("192.0.2.12"), {"mode": "rtu"})
