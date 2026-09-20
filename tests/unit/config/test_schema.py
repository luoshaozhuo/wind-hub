"""Unit tests for pydantic config schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from wind_hub.config.schema import (
    DeviceConfig,
    DevicesConfig,
    PointAddress,
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
    PollingGroup,
    ResolvedPointTable,
    RoutingConfig,
    SinkConfig,
    SystemConfig,
)
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.route import RouteMatch, RouteRule, RouteTarget

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
                        point_table="t1",
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
                        point_table="t1",
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
                    DeviceConfig(device_id="d1", point_table="t1", protocol="modbus", endpoint=ep),
                    DeviceConfig(device_id="d1", point_table="t1", protocol="ads", endpoint=ep),
                ]
            )

    def test_invalid_read_mode_raises(self) -> None:
        with pytest.raises(ConfigError, match="read_mode"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                read_mode="batch",
            )

    def test_valid_read_mode_accepted(self) -> None:
        for read_mode in ("sum", "sequential"):
            cfg = DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                read_mode=read_mode,
            )
            assert cfg.read_mode == read_mode

    def test_mode_field_removed(self) -> None:
        """``mode``（poll/subscribe）已从生产配置移除——多余字段直接报错。"""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                mode="poll",  # type: ignore[call-arg]
            )

    def test_subscribe_field_removed(self) -> None:
        """``subscribe`` 不再是正式配置 Schema 的一部分。"""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                subscribe={"enabled": True},  # type: ignore[call-arg]
            )

    def test_duplicate_polling_group_raises(self) -> None:
        with pytest.raises(ConfigError, match="duplicate polling groups"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="modbus",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                polling=[
                    PollingGroup(group="fast", interval=1.0),
                    PollingGroup(group="fast", interval=2.0),
                ],
            )

    def test_polling_interval_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="interval must be > 0"):
            PollingGroup(group="fast", interval=0.0)
        with pytest.raises(ConfigError, match="interval must be > 0"):
            PollingGroup(group="fast", interval=-1.0)

    def test_supports_scheduled_polling(self) -> None:
        ep = Endpoint(host="10.0.0.1", port=502)
        ads_sum = DeviceConfig(
            device_id="d1", point_table="t1", protocol="ads", endpoint=ep, read_mode="sum"
        )
        ads_seq = DeviceConfig(
            device_id="d2",
            point_table="t1",
            protocol="ads",
            endpoint=ep,
            read_mode="sequential",
        )
        modbus = DeviceConfig(device_id="d3", point_table="t1", protocol="modbus", endpoint=ep)
        assert ads_sum.supports_scheduled_polling is True
        assert ads_seq.supports_scheduled_polling is False
        assert modbus.supports_scheduled_polling is True


# ---------------------------------------------------------------------------
# PointTablesConfig
# ---------------------------------------------------------------------------


class TestPointTablesConfig:
    """Raw 点表模型（``PointTableConfig`` / ``PointPatch``）的原始校验。"""

    def test_duplicate_point_id_in_table_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate"):
            PointTableConfig(
                points=[
                    PointPatch(point_id="p001"),
                    PointPatch(point_id="p001"),
                ]
            )

    def test_duplicate_remove_points_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate remove_points"):
            PointTableConfig(extends="base", remove_points=["p001", "p001"])

    def test_empty_point_id_raises(self) -> None:
        with pytest.raises(ConfigError, match="non-empty"):
            PointPatch(point_id="")

    def test_raw_defaults(self) -> None:
        cfg = PointTableConfig()
        assert cfg.extends is None
        assert cfg.remove_points == []
        assert cfg.points == []

    def test_patch_fields_default_unset(self) -> None:
        """除 point_id 外全部字段默认未写（不在 model_fields_set）。"""
        patch = PointPatch(point_id="p001", max_value=2500.0)
        assert patch.model_fields_set == {"point_id", "max_value"}
        assert patch.unit is None
        assert "unit" not in patch.model_fields_set

    def test_same_point_id_across_tables_ok(self) -> None:
        """point_id 的命名空间是单份点表——不同表之间允许重复。"""
        cfg = PointTablesConfig(
            tables={
                "t1": PointTableConfig(points=[PointPatch(point_id="p001")]),
                "t2": PointTableConfig(points=[PointPatch(point_id="p001")]),
            }
        )
        assert len(cfg.tables) == 2


class TestResolvedPointTable:
    """继承展开后的完整点表（运行模型）校验。"""

    def test_invalid_data_type_raises(self) -> None:
        addr = PointAddress(type="holding_register")
        with pytest.raises(ConfigError, match="data_type"):
            ResolvedPointTable(
                points=[PointConfig(point_id="p1", address=addr, data_type="imaginary")]
            )

    def test_duplicate_point_id_raises(self) -> None:
        addr = PointAddress(type="holding_register")
        with pytest.raises(ConfigError, match="Duplicate"):
            ResolvedPointTable(
                points=[
                    PointConfig(point_id="p001", address=addr, data_type="float32"),
                    PointConfig(point_id="p001", address=addr, data_type="float32"),
                ]
            )

    def test_valid_config_builds(self) -> None:
        cfg = ResolvedPointTable(
            points=[
                PointConfig(
                    point_id="p001",
                    variable_name="rotor_speed",
                    group="fast",
                    address=PointAddress(ioa=1001),
                    data_type="float32",
                    sinks=["kafka_main"],
                ),
                PointConfig(
                    point_id="p002",
                    address=PointAddress(type="measured_value", ioa=1002),
                    data_type="float32",
                ),
            ]
        )
        assert len(cfg.points) == 2
        assert cfg.points[0].sinks == ["kafka_main"]
        assert cfg.points[0].variable_name == "rotor_speed"
        assert cfg.points[0].group == "fast"
        assert cfg.points[1].sinks is None
        assert cfg.points[1].group == "default"

    def test_point_config_has_no_device_id(self) -> None:
        """点是设备无关的——``device_id`` 不再是 PointConfig 的字段。"""
        point = PointConfig(
            point_id="p001", address=PointAddress(ioa=1001), data_type="float32"
        )
        assert not hasattr(point, "device_id")
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PointConfig(
                point_id="p001",
                device_id="wtg-001",  # type: ignore[call-arg]
                address=PointAddress(ioa=1001),
                data_type="float32",
            )


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
            name="r1",
            match=RouteMatch(all=True),
            targets=[RouteTarget(sink="kafka")],
            priority=10,
        )
        r2 = RouteRule(
            name="r1",
            match=RouteMatch(all=True),
            targets=[RouteTarget(sink="file")],
            priority=5,
        )
        with pytest.raises(ConfigError, match="Duplicate"):
            RoutingConfig(rules=[r1, r2])

    def test_empty_rules_ok(self) -> None:
        cfg = RoutingConfig(rules=[])
        assert cfg.rules == []


class TestRouteMatch:
    def test_all_true_accepted(self) -> None:
        m = RouteMatch(all=True)
        assert m.all is True

    def test_single_dimension_accepted(self) -> None:
        assert RouteMatch(device_group="turbine").point_group is None
        assert RouteMatch(point_group="fast").device_group is None

    def test_both_dimensions_accepted(self) -> None:
        m = RouteMatch(device_group="turbine", point_group="fast")
        assert m.device_group == "turbine" and m.point_group == "fast"

    def test_all_true_with_other_matchers_rejected(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            RouteMatch(all=True, device_group="turbine")
        with pytest.raises(ValueError, match="mutually exclusive"):
            RouteMatch(all=True, point_group="fast")

    def test_empty_match_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one condition"):
            RouteMatch()
