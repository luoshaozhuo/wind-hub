"""Inventory 查询与凭据脱敏契约。"""

from __future__ import annotations

import pytest

from cli.config_session import ConfigSession
from cli.inventory import devices, points, sinks


def _snapshot(name: str, config_dir):
    return ConfigSession().read(config_dir(name)).snapshot


def test_devices_effective_endpoint_and_options(config_dir) -> None:
    modbus = devices(_snapshot("modbus", config_dir))
    assert modbus[0]["device_id"] == "modbus-001"
    assert modbus[0]["protocol"] == "modbus"
    assert modbus[0]["host"]
    assert isinstance(modbus[0]["port"], int)
    assert {"slave_id", "word_order", "timeout"} <= set(modbus[0]["protocol_options"])

    iec = devices(_snapshot("iec104", config_dir), "wtg-101")[0]
    assert {"read_timeout", "command_timeout", "interrogation_timeout"} <= set(
        iec["protocol_options"]
    )

    ads = devices(_snapshot("ads", config_dir), "ads-001")[0]
    assert ads["protocol_options"]["local_ams_net_id"] == "127.0.0.1.1.1"


def test_devices_unknown_raises(config_dir) -> None:
    with pytest.raises(ValueError, match="unknown device"):
        devices(_snapshot("modbus", config_dir), "ghost")


def test_points_list_and_single(config_dir) -> None:
    snapshot = _snapshot("modbus", config_dir)
    all_points = points(snapshot, "modbus-001")
    assert {"active_power", "wind_speed"} <= {item["point_id"] for item in all_points}
    single = points(snapshot, "modbus-001", "active_power")
    assert len(single) == 1
    assert single[0]["access"] == "read"
    assert isinstance(single[0]["address"], dict)


def test_points_unknown_device_and_point(config_dir) -> None:
    snapshot = _snapshot("modbus", config_dir)
    with pytest.raises(ValueError, match="unknown device"):
        points(snapshot, "ghost")
    with pytest.raises(ValueError, match="unknown point"):
        points(snapshot, "modbus-001", "ghost")


def test_sinks_hide_credentials(config_dir) -> None:
    import yaml

    base = config_dir("modbus")
    secret_password = "S3CR3T-redacted-password"
    sinks_doc = yaml.safe_load((base / "sinks.yaml").read_text())
    conn = sinks_doc["sinks"]["redis_latest"]["connection"]
    conn["username"] = "turbine_operator"
    conn["password"] = secret_password
    (base / "sinks.yaml").write_text(yaml.safe_dump(sinks_doc))

    snapshot = ConfigSession().read(base).snapshot
    result = sinks(snapshot)
    assert result
    rendered = repr(result)
    assert "password" not in rendered
    assert "username" not in rendered
    assert "turbine_operator" not in rendered
    assert secret_password not in rendered
    assert "key_prefix" not in rendered
    connection = result[0]["connection"]
    assert {"host", "port", "database"} <= set(connection)


def test_sinks_unknown_raises(config_dir) -> None:
    with pytest.raises(ValueError, match="unknown sink"):
        sinks(_snapshot("modbus", config_dir), "ghost")


def test_gateways_command_not_defined(config_dir) -> None:
    from cli.main import WindHubShell

    shell = WindHubShell()
    shell.config.read(config_dir("modbus"))
    result = shell.dispatch(["gateways"])
    assert isinstance(result, dict)
    assert result["status"] == "UNKNOWN"
    assert result["code"] == "NOT_DEFINED"
    assert result["items"] == []
    # 至多接受一个 Gateway ID；help 与 dispatch 语法一致。
    one = shell.dispatch(["gateways", "gw-1"])
    assert isinstance(one, dict) and one["gateway_id"] == "gw-1"
    with pytest.raises(ValueError, match="usage: gateways"):
        shell.dispatch(["gateways", "gw-1", "gw-2"])


def test_inventory_requires_loaded_config() -> None:
    from cli.main import WindHubShell

    with pytest.raises(Exception, match="not loaded"):
        WindHubShell().dispatch(["devices"])
