"""Unit tests for ProtocolRegistry and driver self-registration."""

from __future__ import annotations

import pytest

import wind_hub.adapter.outbound.protocol  # noqa: F401 — trigger self-registration
from wind_hub_core.config.schema import DeviceConfig, Endpoint
from wind_hub_core.model.errors import ConfigError
from wind_hub.domain.port.outbound import HealthStatus
from wind_hub.infra.protocol_registry import (
    ProtocolRegistry,
    protocol_registry,
    register_protocol,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_device_cfg(
    device_id: str = "test-dev",
    protocol: str = "ads",
    host: str = "127.0.0.1",
    port: int = 851,
) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        endpoint=Endpoint(host=host, port=port),
        point_table="t1",
    )


# ---------------------------------------------------------------------------
# fresh-registry tests
# ---------------------------------------------------------------------------


class TestFreshRegistry:
    """Tests against a freshly-created (non-global) registry."""

    def test_register_driver_success(self) -> None:
        reg = ProtocolRegistry()
        reg.register("test", lambda cfg: "fake-driver")  # type: ignore[arg-type,return-value]
        assert reg.is_registered("test")

    def test_register_duplicate_raises(self) -> None:
        reg = ProtocolRegistry()
        reg.register("test", lambda cfg: "fake-driver")  # type: ignore[arg-type,return-value]
        with pytest.raises(ConfigError, match="already registered"):
            reg.register("test", lambda cfg: "other")  # type: ignore[arg-type,return-value]

    def test_create_registered_returns_instance(self) -> None:
        reg = ProtocolRegistry()
        reg.register("test", lambda cfg: f"driver-{cfg.device_id}")
        cfg = _make_device_cfg()
        driver = reg.create("test", cfg)
        assert driver == "driver-test-dev"

    def test_create_unknown_raises(self) -> None:
        reg = ProtocolRegistry()
        cfg = _make_device_cfg()
        with pytest.raises(ConfigError, match="Unknown protocol driver 'unknown'"):
            reg.create("unknown", cfg)

    def test_names_returns_sorted_list(self) -> None:
        reg = ProtocolRegistry()
        reg.register("z", lambda _: "z")  # type: ignore[arg-type,return-value]
        reg.register("a", lambda _: "a")  # type: ignore[arg-type,return-value]
        assert reg.names() == ["a", "z"]

    def test_is_registered(self) -> None:
        reg = ProtocolRegistry()
        reg.register("foo", lambda _: "bar")  # type: ignore[arg-type,return-value]
        assert reg.is_registered("foo") is True
        assert reg.is_registered("bar") is False


# ---------------------------------------------------------------------------
# global registry tests
# ---------------------------------------------------------------------------


class TestGlobalRegistry:
    """Tests against the global singleton (drivers already registered)."""

    def test_three_drivers_registered(self) -> None:
        names = protocol_registry.names()
        assert "ads" in names
        assert "modbus" in names
        assert "iec104" in names

    def test_create_all_three_returns_instances(self) -> None:
        for proto_name in ("ads", "modbus", "iec104"):
            cfg = _make_device_cfg(protocol=proto_name)
            driver = protocol_registry.create(proto_name, cfg)
            assert driver is not None

    def test_all_drivers_health_not_connected(self) -> None:
        for proto_name in ("ads", "modbus", "iec104"):
            cfg = _make_device_cfg(protocol=proto_name)
            driver = protocol_registry.create(proto_name, cfg)
            status = driver.health()
            assert isinstance(status, HealthStatus)
            assert status.healthy is False  # not connected by default
            assert status.message == "not connected"


# ---------------------------------------------------------------------------
# register_protocol decorator
# ---------------------------------------------------------------------------


def test_decorator_registers_factory() -> None:
    @register_protocol("deco-test")
    def _factory(cfg: DeviceConfig) -> str:  # type: ignore[return-type]
        return f"deco-{cfg.device_id}"

    # The decorator registered into the global singleton — verify
    assert protocol_registry.is_registered("deco-test")

    # Clean up after ourselves (remove from global singleton)
    # We can't easily remove, but at least verify it's there
    driver = protocol_registry.create("deco-test", _make_device_cfg())
    assert driver == "deco-test-dev"
