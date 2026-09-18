"""Unit tests for Modbus config parsing and validation."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.modbus.config import from_device_config
from wind_hub.config.schema import DeviceConfig, Endpoint
from wind_hub.domain.model.errors import ConfigError


def _cfg(**extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-dev",
        protocol="modbus",
        point_table="t1",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=502,
            extensions=dict(extensions),
        ),
    )


def test_defaults() -> None:
    c = from_device_config(_cfg())
    assert c.host == "127.0.0.1"
    assert c.port == 502
    assert c.unit_id == 1
    assert c.mode == "tcp"
    assert c.timeout == 5.0
    assert c.reconnect_max_retries == 5
    assert c.reconnect_backoff_max == 30.0
    assert c.byte_order == "big_endian"


def test_custom_values() -> None:
    c = from_device_config(
        _cfg(
            mode="rtu",
            unit_id=7,
            timeout=2.5,
            byte_order="little_endian",
            reconnect_max_retries=3,
            reconnect_backoff_max=10.0,
        )
    )
    assert c.mode == "rtu"
    assert c.unit_id == 7
    assert c.timeout == 2.5
    assert c.byte_order == "little_endian"
    assert c.reconnect_max_retries == 3
    assert c.reconnect_backoff_max == 10.0


def test_port_override() -> None:
    c = from_device_config(_cfg(port=1502))
    assert c.port == 1502


def test_rtu_mode_is_accepted_by_config() -> None:
    """RTU parses fine at config time (the driver rejects it at connect)."""
    c = from_device_config(_cfg(mode="rtu"))
    assert c.mode == "rtu"


def test_invalid_mode_raises() -> None:
    with pytest.raises(ConfigError, match="invalid mode"):
        from_device_config(_cfg(mode="serial"))


@pytest.mark.parametrize("unit_id", [-1, 256, 1000])
def test_invalid_unit_id_raises(unit_id: int) -> None:
    with pytest.raises(ConfigError, match="unit_id"):
        from_device_config(_cfg(unit_id=unit_id))


@pytest.mark.parametrize("timeout", [0, -1, -0.5])
def test_non_positive_timeout_raises(timeout: float) -> None:
    with pytest.raises(ConfigError, match="timeout"):
        from_device_config(_cfg(timeout=timeout))


def test_invalid_byte_order_raises() -> None:
    with pytest.raises(ConfigError, match="byte_order"):
        from_device_config(_cfg(byte_order="middle_endian"))
