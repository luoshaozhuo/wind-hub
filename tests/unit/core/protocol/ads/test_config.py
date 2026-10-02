"""Unit tests for ADS config parsing and validation."""

from __future__ import annotations

import pytest

from wind_hub_core.config.schema import DeviceConfig, Endpoint
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.protocol.ads.config import from_device_config


def _cfg(**extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-dev",
        protocol="ads",
        point_table="t1",
        endpoint=Endpoint(
            host="192.168.0.100",
            port=48898,
            extensions=dict(extensions),
        ),
    )


def test_defaults() -> None:
    c = from_device_config(_cfg())
    assert c.target_net_id == ""
    assert c.twincat_version == "2"
    assert c.target_port == 801
    assert c.timeout == 5.0
    assert c.reconnect_max_retries == 5
    assert c.reconnect_backoff_max == 30.0


def test_custom_target_net_id() -> None:
    c = from_device_config(_cfg(target_net_id="192.168.0.100.1.1"))
    assert c.target_net_id == "192.168.0.100.1.1"


def test_ams_port_alias() -> None:
    c = from_device_config(_cfg(target_net_id="1.1.1.1.1.1", ams_port=852))
    assert c.target_port == 852


def test_target_port_override() -> None:
    c = from_device_config(_cfg(target_net_id="1.1.1.1.1.1", target_port=900))
    assert c.target_port == 900


def test_twincat_version_3_defaults_port_851() -> None:
    c = from_device_config(_cfg(twincat_version="3"))
    assert c.twincat_version == "3"
    assert c.target_port == 851


def test_twincat_version_2_defaults_port_801() -> None:
    c = from_device_config(_cfg(twincat_version="2"))
    assert c.twincat_version == "2"
    assert c.target_port == 801


def test_twincat_version_port_can_be_overridden() -> None:
    c = from_device_config(_cfg(twincat_version="2", target_port=900))
    assert c.target_port == 900


def test_invalid_twincat_version_raises() -> None:
    with pytest.raises(ConfigError, match="twincat_version"):
        from_device_config(_cfg(twincat_version="4"))


def test_read_mode_defaults_sum() -> None:
    assert from_device_config(_cfg()).read_mode == "sum"


def test_read_mode_sequential() -> None:
    cfg = DeviceConfig(
        device_id="test-dev",
        protocol="ads",
        point_table="t1",
        endpoint=Endpoint(host="192.168.0.100", port=48898),
        read_mode="sequential",
    )
    assert from_device_config(cfg).read_mode == "sequential"


@pytest.mark.parametrize("net_id", ["not-a-net-id", "1.2.3", "1.2.3.4.5.6.7", "a.b.c.d.e.f"])
def test_invalid_target_net_id_raises(net_id: str) -> None:
    with pytest.raises(ConfigError, match="target_net_id"):
        from_device_config(_cfg(target_net_id=net_id))


def test_empty_net_id_allowed() -> None:
    """A driver may be instantiated for a health check without a Net ID."""
    c = from_device_config(_cfg())
    assert c.target_net_id == ""


# ---------------------------------------------------------------------------
# 进程级 ADS 本机配置（system.yaml 的 ads 段）
# ---------------------------------------------------------------------------


def test_system_ads_config_parsing() -> None:
    from wind_hub_core.config.schema import SystemConfig

    sc = SystemConfig(
        ads={
            "local_ams_net_id": "192.168.151.244.1.2",
            "local_ip": "192.168.151.244",
            "username": "Administrator",
            "password": "",
        }
    )
    assert sc.ads is not None
    assert sc.ads.username == "Administrator"
    assert sc.ads.password == ""


def test_system_ads_config_defaults() -> None:
    from wind_hub_core.config.schema import SystemConfig

    assert SystemConfig().ads is None
    sc = SystemConfig(
        ads={"local_ams_net_id": "192.168.151.244.1.2", "local_ip": "192.168.151.244"}
    )
    assert sc.ads is not None
    assert sc.ads.username == "Administrator"
    assert sc.ads.password == ""

def test_system_ads_invalid_local_net_id_raises() -> None:
    from wind_hub_core.config.schema import SystemConfig

    with pytest.raises(ConfigError, match="AMS Net ID"):
        SystemConfig(ads={"local_ams_net_id": "bad", "local_ip": "192.168.151.244"})
