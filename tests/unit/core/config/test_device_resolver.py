"""Unit tests for resolver/device — DeviceInstance + DeviceModel → DeviceConfig."""

from __future__ import annotations

import pytest

from wind_hub_core.config import (
    DeviceInstancesConfig,
    DeviceModelsConfig,
    resolve_devices,
)
from wind_hub_core.model.errors import ConfigError


def _models(**models: dict) -> DeviceModelsConfig:
    return DeviceModelsConfig(
        device_types={"turbine": {"name": "风机"}},
        device_models={
            mid: {"device_type": "turbine", **body} for mid, body in models.items()
        },
    )


def _instances(*devices: dict) -> DeviceInstancesConfig:
    return DeviceInstancesConfig(devices=list(devices))


class TestResolveDevices:
    def test_resolve_merges_model_into_instance(self) -> None:
        """正常 resolve：协议/点表/read_mode/device_type 来自型号。"""
        models = _models(
            beckhoff_2mw={
                "protocol": "ads",
                "point_table": "t1",
                "read_mode": "sum",
                "properties": {"rated_power_kw": 2000},
                "connection_defaults": {"port": 801, "twincat_version": "2"},
            }
        )
        instances = _instances(
            {
                "device_id": "wtg-001",
                "model": "beckhoff_2mw",
                "device_group": "turbine_ads",
                "endpoint": {
                    "host": "192.168.52.101",
                    "extensions": {"target_net_id": "192.168.52.101.1.1"},
                },
            }
        )
        devices = resolve_devices(instances, models)
        d = devices["wtg-001"]
        assert d.device_id == "wtg-001"
        assert d.device_type == "turbine"
        assert d.model == "beckhoff_2mw"
        assert d.protocol == "ads"
        assert d.point_table == "t1"
        assert d.read_mode == "sum"
        assert d.device_group == "turbine_ads"
        assert d.enabled is True
        assert d.endpoint.host == "192.168.52.101"
        assert d.endpoint.port == 801
        assert d.endpoint.extensions == {
            "twincat_version": "2",
            "target_net_id": "192.168.52.101.1.1",
        }

    def test_unknown_model_raises(self) -> None:
        models = _models(m1={"protocol": "modbus", "point_table": "t1"})
        instances = _instances(
            {"device_id": "d1", "model": "ghost", "endpoint": {"host": "h", "port": 1}}
        )
        with pytest.raises(ConfigError, match="d1.*ghost"):
            resolve_devices(instances, models)

    def test_instance_endpoint_overrides_connection_defaults(self) -> None:
        models = _models(
            m1={
                "protocol": "modbus",
                "point_table": "t1",
                "connection_defaults": {"port": 502, "unit_id": 1, "timeout": 3.0},
            }
        )
        instances = _instances(
            {
                "device_id": "d1",
                "model": "m1",
                "endpoint": {"host": "h", "port": 1502, "extensions": {"unit_id": 9}},
            }
        )
        ep = resolve_devices(instances, models)["d1"].endpoint
        assert ep.port == 1502
        assert ep.extensions == {"unit_id": 9, "timeout": 3.0}

    def test_port_falls_back_to_connection_defaults(self) -> None:
        models = _models(
            m1={
                "protocol": "modbus",
                "point_table": "t1",
                "connection_defaults": {"port": 502},
            }
        )
        instances = _instances({"device_id": "d1", "model": "m1", "endpoint": {"host": "h"}})
        assert resolve_devices(instances, models)["d1"].endpoint.port == 502

    def test_missing_port_everywhere_raises(self) -> None:
        models = _models(m1={"protocol": "modbus", "point_table": "t1"})
        instances = _instances({"device_id": "d1", "model": "m1", "endpoint": {"host": "h"}})
        with pytest.raises(ConfigError, match="d1.*port"):
            resolve_devices(instances, models)

    def test_ads_read_mode_defaults_to_sum(self) -> None:
        models = _models(m1={"protocol": "ads", "point_table": "t1"})
        instances = _instances(
            {"device_id": "d1", "model": "m1", "endpoint": {"host": "h", "port": 801}}
        )
        assert resolve_devices(instances, models)["d1"].read_mode == "sum"


class TestDeviceModelsConfigValidation:
    def test_unknown_device_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="m1.*ghost"):
            DeviceModelsConfig(
                device_types={"turbine": {}},
                device_models={
                    "m1": {"device_type": "ghost", "protocol": "modbus", "point_table": "t1"}
                },
            )

    def test_unknown_protocol_raises(self) -> None:
        with pytest.raises(ConfigError, match="opcua"):
            DeviceModelsConfig(
                device_types={"turbine": {}},
                device_models={
                    "m1": {"device_type": "turbine", "protocol": "opcua", "point_table": "t1"}
                },
            )

    def test_read_mode_rejected_for_non_ads(self) -> None:
        with pytest.raises(ConfigError, match="read_mode"):
            DeviceModelsConfig(
                device_types={"turbine": {}},
                device_models={
                    "m1": {
                        "device_type": "turbine",
                        "protocol": "modbus",
                        "point_table": "t1",
                        "read_mode": "sum",
                    }
                },
            )

    def test_ads_read_mode_must_be_known(self) -> None:
        with pytest.raises(ConfigError, match="read_mode"):
            DeviceModelsConfig(
                device_types={"turbine": {}},
                device_models={
                    "m1": {
                        "device_type": "turbine",
                        "protocol": "ads",
                        "point_table": "t1",
                        "read_mode": "parallel",
                    }
                },
            )
