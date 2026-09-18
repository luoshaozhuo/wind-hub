"""Unit tests for pydantic config schema validation."""

from __future__ import annotations

import pytest

from wind_hub.config.schema import (
    DeviceConfig,
    DevicesConfig,
    PointAddress,
    PointConfig,
    PointsConfig,
    RoutingConfig,
    SinkConfig,
    SystemConfig,
)
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.route import RouteRule

# ---------------------------------------------------------------------------
# SystemConfig
# ---------------------------------------------------------------------------


class TestSinkConfig:
    def test_duplicate_sink_names_raises(self) -> None:
        sinks = [
            SinkConfig(name="kafka", type="kafka"),
            SinkConfig(name="kafka", type="kafka"),
        ]
        with pytest.raises(ConfigError, match="Duplicate"):
            SystemConfig(sinks=sinks)

    def test_unique_sink_names_ok(self) -> None:
        sinks = [
            SinkConfig(name="kafka", type="kafka"),
            SinkConfig(name="file", type="file"),
        ]
        cfg = SystemConfig(sinks=sinks)
        assert len(cfg.sinks) == 2

    def test_defaults(self) -> None:
        cfg = SystemConfig()
        assert cfg.scheduler.default_interval == 1.0
        assert cfg.scheduler.max_concurrent_devices == 32
        assert cfg.pipeline.processors == []
        assert cfg.sinks == []
        assert cfg.interfaces.api.port == 8080


# ---------------------------------------------------------------------------
# DevicesConfig
# ---------------------------------------------------------------------------


class TestDevicesConfig:
    def test_protocol_whitelist_rejects_bad_value(self) -> None:
        with pytest.raises(ConfigError, match="opcua"):
            DevicesConfig(
                devices=[
                    DeviceConfig(
                        device_id="d1",
                        protocol="opcua",
                        endpoint=Endpoint(host="10.0.0.1", port=4840),
                    )
                ]
            )

    def test_allowed_protocols_accepted(self) -> None:
        for proto in ("ads", "modbus", "iec104"):
            cfg = DevicesConfig(
                devices=[
                    DeviceConfig(
                        device_id="d1",
                        protocol=proto,
                        endpoint=Endpoint(host="10.0.0.1", port=502),
                    )
                ]
            )
            assert cfg.devices[0].protocol == proto

    def test_duplicate_device_id_raises(self) -> None:
        ep = Endpoint(host="10.0.0.1", port=502)
        with pytest.raises(ConfigError, match="Duplicate"):
            DevicesConfig(
                devices=[
                    DeviceConfig(device_id="d1", protocol="modbus", endpoint=ep),
                    DeviceConfig(device_id="d1", protocol="ads", endpoint=ep),
                ]
            )

    def test_invalid_read_mode_raises(self) -> None:
        with pytest.raises(ConfigError, match="read_mode"):
            DeviceConfig(
                device_id="d1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                read_mode="batch",
            )

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ConfigError, match="mode"):
            DeviceConfig(
                device_id="d1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                mode="push",
            )

    def test_valid_read_mode_and_mode_accepted(self) -> None:
        for read_mode in ("sum", "sequential"):
            for mode in ("poll", "subscribe", "both"):
                cfg = DeviceConfig(
                    device_id="d1",
                    protocol="ads",
                    endpoint=Endpoint(host="10.0.0.1", port=502),
                    read_mode=read_mode,
                    mode=mode,
                )
                assert cfg.read_mode == read_mode
                assert cfg.mode == mode

    def test_subscribe_defaults(self) -> None:
        cfg = DeviceConfig(
            device_id="d1",
            protocol="ads",
            endpoint=Endpoint(host="10.0.0.1", port=502),
        )
        assert cfg.subscribe.enabled is False
        assert cfg.subscribe.cycle_time == 0.02
        assert cfg.subscribe.max_delay == 0.06
        assert cfg.subscribe.max_notifications_per_connection == 550


# ---------------------------------------------------------------------------
# PointsConfig
# ---------------------------------------------------------------------------


class TestPointsConfig:
    def test_duplicate_device_point_id_raises(self) -> None:
        addr = PointAddress(type="holding_register")
        with pytest.raises(ConfigError, match="Duplicate"):
            PointsConfig(
                points=[
                    PointConfig(
                        point_id="rotor.speed",
                        device_id="wtg-001",
                        address=addr,
                        data_type="float32",
                    ),
                    PointConfig(
                        point_id="rotor.speed",
                        device_id="wtg-001",
                        address=addr,
                        data_type="float32",
                    ),
                ]
            )

    def test_different_device_same_point_ok(self) -> None:
        addr = PointAddress(type="holding_register")
        cfg = PointsConfig(
            points=[
                PointConfig(
                    point_id="rotor.speed",
                    device_id="wtg-001",
                    address=addr,
                    data_type="float32",
                ),
                PointConfig(
                    point_id="rotor.speed",
                    device_id="wtg-002",
                    address=addr,
                    data_type="float32",
                ),
            ]
        )
        assert len(cfg.points) == 2

    def test_invalid_data_type_raises(self) -> None:
        addr = PointAddress(type="holding_register")
        with pytest.raises(ConfigError, match="data_type"):
            PointsConfig(
                points=[
                    PointConfig(
                        point_id="p1",
                        device_id="d1",
                        address=addr,
                        data_type="imaginary",
                    )
                ]
            )

    def test_valid_config_builds(self) -> None:
        cfg = PointsConfig(
            points=[
                PointConfig(
                    point_id="rotor.speed",
                    device_id="wtg-001",
                    address=PointAddress(ioa=1001),
                    data_type="float32",
                    sinks=["kafka_main"],
                ),
                PointConfig(
                    point_id="gen.power",
                    device_id="wtg-001",
                    address=PointAddress(type="measured_value", ioa=1002),
                    data_type="float32",
                ),
            ]
        )
        assert len(cfg.points) == 2
        assert cfg.points[0].sinks == ["kafka_main"]
        assert cfg.points[1].sinks is None


class TestPointAddress:
    def test_extra_fields_allowed(self) -> None:
        """PointAddress allows arbitrary keys for protocol-specific addresses."""
        addr = PointAddress(type="holding_register", register=30001, slave=1)
        assert addr.type == "holding_register"
        assert addr.register == 30001  # type: ignore[attr-defined]
        assert addr.slave == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# RoutingConfig
# ---------------------------------------------------------------------------


class TestRoutingConfig:
    def test_duplicate_rule_names_raises(self) -> None:
        r1 = RouteRule(
            name="r1", targets=["kafka"], priority=10, match_device=None, match_point_prefix=None
        )
        r2 = RouteRule(
            name="r1", targets=["file"], priority=5, match_device=None, match_point_prefix=None
        )
        with pytest.raises(ConfigError, match="Duplicate"):
            RoutingConfig(rules=[r1, r2])

    def test_empty_rules_ok(self) -> None:
        cfg = RoutingConfig(rules=[])
        assert cfg.rules == []
