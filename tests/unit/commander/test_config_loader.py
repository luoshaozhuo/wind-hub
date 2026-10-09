"""新 Commander 配置加载（Infrastructure config adapter）单元测试。"""

from __future__ import annotations

import pytest

from commander.infrastructure.config import load_commander_config
from core.application import ConfigError
from core.domain import DeviceId, PointAccess, PointTableId
from core.infrastructure.config import fingerprint_config_set
from tests.support.new_commander import (
    write_minimal_config_tree,
    write_yaml,
)


def test_load_minimal_modbus_config(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    config = load_commander_config(config_dir)

    device = config.devices[DeviceId("dev1")]
    assert device.endpoint.host == "127.0.0.1"
    assert device.endpoint.port == 502
    table = config.point_table_for_device(device.device_id)
    assert table.protocol.name == "modbus"
    assert "p1" in table.points
    assert config.connect_timeout == 10.0
    assert config.write_timeout == 5.0


def test_endpoint_merge_defaults_and_instance_wins(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        protocol="modbus",
        connection_defaults={"port": 1502, "unit_id": 3, "word_order": "little_endian"},
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {
                    "host": "10.0.0.1",
                    "extensions": {"unit_id": 9},
                },
            }
        ],
    )
    config = load_commander_config(config_dir)
    device = config.devices[DeviceId("dev1")]
    assert device.endpoint.port == 1502
    options = config.protocol_options_for(device.device_id)
    assert options["unit_id"] == 9  # 实例 extensions 覆盖型号默认值
    assert options["word_order"] == "little_endian"


def test_missing_port_is_config_error(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "10.0.0.1"},
            }
        ],
    )
    with pytest.raises(ConfigError, match="port"):
        load_commander_config(config_dir)


def test_unknown_model_reference_rejected(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "ghost",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        ],
    )
    with pytest.raises(ConfigError, match="unknown model"):
        load_commander_config(config_dir)


def test_unknown_point_table_reference_rejected(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "device_models.yaml",
        {
            "device_types": {"turbine": {"name": "风机"}},
            "device_models": {
                "mod": {
                    "device_type": "turbine",
                    "protocol": "modbus",
                    "point_table": "missing",
                }
            },
        },
    )
    with pytest.raises(ConfigError, match="unknown point_table"):
        load_commander_config(config_dir)


def test_model_protocol_must_match_table_protocol(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "device_models.yaml",
        {
            "device_types": {"turbine": {"name": "风机"}},
            "device_models": {
                "mod": {
                    "device_type": "turbine",
                    "protocol": "iec104",
                    "point_table": "tab",
                }
            },
        },
    )
    with pytest.raises(ConfigError, match="does not match"):
        load_commander_config(config_dir)


def test_unknown_unit_rejected(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        points=[
            {
                "point_id": "p1",
                "point_groups": ["g"],
                "address": {"register_type": "holding", "address": 100},
                "data_type": "float32",
                "unit": "furlong",
            }
        ],
    )
    with pytest.raises(ConfigError, match="unknown unit"):
        load_commander_config(config_dir)


def test_bool_point_must_be_dimensionless(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "units.yaml",
        {"units": {"percent": {"symbol": "%", "name": "Percent"}}},
    )
    write_yaml(
        config_dir,
        "points.yaml",
        {
            "point_tables": {
                "tab": {
                    "protocol": "modbus",
                    "points": [
                        {
                            "point_id": "flag",
                            "point_groups": ["g"],
                            "address": {"register_type": "coil", "address": 1},
                            "data_type": "bool",
                            "unit": "percent",
                        }
                    ],
                }
            }
        },
    )
    with pytest.raises(ConfigError, match="dimensionless"):
        load_commander_config(config_dir)


def test_access_derivation_modbus(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        points=[
            {
                "point_id": "ro",
                "point_groups": ["g"],
                "address": {"register_type": "input", "address": 100},
                "data_type": "float32",
            },
            {
                "point_id": "rw",
                "point_groups": ["g"],
                "address": {"register_type": "holding", "address": 200},
                "data_type": "float32",
            },
        ],
    )
    config = load_commander_config(config_dir)
    table = config.point_tables[PointTableId("tab")]
    assert table.points["ro"].access is PointAccess.READ
    assert table.points["rw"].access is PointAccess.READ_WRITE


def test_access_derivation_iec104(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        protocol="iec104",
        points=[
            {
                "point_id": "meas",
                "point_groups": ["g"],
                "address": {"ioa": 100, "type": "M_ME_NC_1"},
                "data_type": "float32",
            },
            {
                "point_id": "cmd",
                "point_groups": ["g"],
                "address": {"ioa": 200, "type": "C_SC_NA_1"},
                "data_type": "bool",
            },
        ],
    )
    config = load_commander_config(config_dir)
    table = config.point_tables[PointTableId("tab")]
    assert table.points["meas"].access is PointAccess.READ
    assert table.points["cmd"].access is PointAccess.READ_WRITE


def test_ads_read_mode_injected_into_options(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        protocol="ads",
        address={"symbol": "MAIN.n", "data_type": "float32"},
        connection_defaults={"target_net_id": "1.2.3.4.5.6"},
        read_mode="sequential",
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "10.0.0.1", "port": 851},
            }
        ],
    )
    config = load_commander_config(config_dir)
    options = config.protocol_options_for(DeviceId("dev1"))
    assert options["read_mode"] == "sequential"
    assert options["target_net_id"] == "1.2.3.4.5.6"


def test_business_point_conflict_gets_table_prefix(tmp_path):
    """两张表同名点但 data_type 冲突 → 后解析表使用 ``{table}:{point}``。"""
    config_dir = write_minimal_config_tree(
        tmp_path,
        point_tables={
            "tab": {
                "protocol": "modbus",
                "points": [
                    {
                        "point_id": "p1",
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 1},
                        "data_type": "float32",
                    }
                ],
            },
            "tab_b": {
                "protocol": "modbus",
                "points": [
                    {
                        "point_id": "p1",
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 2},
                        "data_type": "int16",
                    }
                ],
            },
        },
    )
    config = load_commander_config(config_dir)
    bp_ids = {str(bp) for bp in config.business_points}
    assert "p1" in bp_ids
    # dict 顺序：tab 先解析占用裸 id，tab_b 冲突后加表前缀
    assert "tab_b:p1" in bp_ids


def test_disabled_devices_excluded_from_snapshot(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            },
            {
                "device_id": "dev2",
                "model": "mod",
                "endpoint": {"host": "10.0.0.2", "port": 502},
                "enabled": False,
            },
        ],
    )
    config = load_commander_config(config_dir)
    assert set(config.devices) == {DeviceId("dev1")}
    assert config.disabled_devices == frozenset({DeviceId("dev2")})


def test_device_group_synthesized(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "device_group": "wind_turbines",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        ],
    )
    config = load_commander_config(config_dir)
    device = config.devices[DeviceId("dev1")]
    assert [str(g) for g in device.device_group_ids] == ["wind_turbines"]
    assert "wind_turbines" in {str(g) for g in config.device_groups}


def test_point_meta_collected(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        points=[
            {
                "point_id": "p1",
                "variable_name": "有功功率",
                "point_groups": ["telemetry", "fast"],
                "address": {"register_type": "holding", "address": 100},
                "data_type": "float32",
            }
        ],
    )
    config = load_commander_config(config_dir)
    meta = config.meta_for(PointTableId("tab"), "p1")
    assert meta.variable_name == "有功功率"
    assert meta.point_groups == ("telemetry", "fast")


def test_unsupported_protocol_rejected(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "points.yaml",
        {
            "point_tables": {
                "tab": {
                    "protocol": "opcua",
                    "points": [
                        {
                            "point_id": "p1",
                            "point_groups": ["g"],
                            "address": {"node": "x"},
                            "data_type": "float32",
                        }
                    ],
                }
            }
        },
    )
    with pytest.raises(ConfigError, match="protocol"):
        load_commander_config(config_dir)


def test_fingerprint_stable_and_history_skipped(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    first = fingerprint_config_set(config_dir)
    assert first == fingerprint_config_set(config_dir)
    history = config_dir / ".history"
    history.mkdir()
    write_yaml(history, "points.yaml", {"point_tables": {}})
    assert fingerprint_config_set(config_dir) == first


def test_ads_local_identity_loaded(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        ads={"local_ams_net_id": "5.6.7.8.1.1", "local_ip": "192.168.1.10"},
    )
    config = load_commander_config(config_dir)
    assert config.ads_local is not None
    assert config.ads_local.local_ams_net_id == "5.6.7.8.1.1"
    assert config.ads_local.local_ip == "192.168.1.10"


def test_invalid_ams_net_id_rejected(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        ads={"local_ams_net_id": "not-a-net-id", "local_ip": "192.168.1.10"},
    )
    with pytest.raises(ConfigError, match="AMS Net ID"):
        load_commander_config(config_dir)


def test_commander_load_ignores_missing_or_invalid_unrelated_topics(tmp_path):
    """Commander 不解析 tasks/sinks；其缺失或非法均不影响加载。"""
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(config_dir, "tasks.yaml", {"tasks": [{"task_id": "bad"}]})
    write_yaml(config_dir, "sinks.yaml", {"sinks": [{"name": "bad", "type": "invalid"}]})
    config = load_commander_config(config_dir)
    assert config.devices
    (config_dir / "tasks.yaml").unlink()
    (config_dir / "sinks.yaml").unlink()
    config = load_commander_config(config_dir)
    assert config.devices
