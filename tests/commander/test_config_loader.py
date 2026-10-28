"""新 Commander 配置加载（Infrastructure config adapter）单元测试。"""

from __future__ import annotations

import pytest

from commander.infrastructure.config import load_commander_config
from core.application import ConfigError
from core.domain import DeviceId, PointAccess, PointTableId
from core.infrastructure.config import YamlConfigAdapter, config_dir_digest
from tests.support.new_commander import (
    write_minimal_config_tree,
    write_yaml,
)

pytestmark = pytest.mark.unit


def test_load_minimal_modbus_config(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    config = load_commander_config(YamlConfigAdapter(config_dir))

    device = config.devices[DeviceId("dev1")]
    assert device.endpoint.host == "127.0.0.1"
    assert device.endpoint.port == 502
    table = config.point_table_for_device(device.device_id)
    assert table.protocol.name == "modbus"
    assert "p1" in table.points
    assert config.connect_timeout == 1.0
    assert config.write_timeout == 1.0


def test_endpoint_merge_defaults_and_instance_wins(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        protocol="modbus",
        endpoint_defaults={"port": 1502},
        protocol_options={"slave_id": 3, "word_order": "little_endian"},
        devices={
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "10.0.0.1"},
                "protocol_options": {"slave_id": 9},
            }
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    device = config.devices[DeviceId("dev1")]
    assert device.endpoint.port == 1502  # 型号 endpoint_defaults 回填有效端口
    options = config.protocol_options_for(device.device_id)
    assert options["slave_id"] == 9  # 设备显式 protocol_options 覆盖型号默认值
    assert options["word_order"] == "little_endian"


def test_missing_port_falls_back_to_protocol_default(tmp_path):
    """设备与型号均未声明端口时，有效端口回退到协议代码默认（Modbus 502）。"""
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices={
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "10.0.0.1"},
            }
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    assert config.devices[DeviceId("dev1")].endpoint.port == 502


def test_unknown_model_reference_rejected(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices={
            "dev1": {
                "model_id": "ghost",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        },
    )
    with pytest.raises(ConfigError, match="unknown model"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_unknown_point_table_reference_rejected(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "device_models.yaml",
        {
            "device_types": {"turbine": {"name": "风机"}},
            "device_models": {
                "mod": {
                    "device_type_id": "turbine",
                    "protocol": "modbus",
                    "point_table_id": "missing",
                }
            },
        },
    )
    with pytest.raises(ConfigError, match="point table"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_model_protocol_must_match_table_protocol(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "device_models.yaml",
        {
            "device_types": {"turbine": {"name": "风机"}},
            "device_models": {
                "mod": {
                    "device_type_id": "turbine",
                    "protocol": "iec104",
                    "point_table_id": "tab",
                }
            },
        },
    )
    with pytest.raises(ConfigError, match="does not match"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_unknown_unit_rejected(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        points={
            "p1": {
                "point_groups": ["g"],
                "address": {"register_type": "holding", "address": 100},
                "data_type": "float32",
                "unit": "furlong",
            }
        },
    )
    with pytest.raises(ConfigError, match="invalid business point|invalid data type or unit"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_bool_point_must_be_dimensionless(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "business_points.yaml",
        {"business_points": {"flag": {"data_type": "bool", "unit": "percent"}}},
    )
    write_yaml(
        config_dir,
        "points.yaml",
        {
            "point_tables": {
                "tab": {
                    "protocol": "modbus",
                    "points": {
                        "flag": {
                            "point_groups": ["g"],
                            "address": {"register_type": "coil", "address": 1},
                            "data_type": "bool",
                            "unit": "percent",
                        }
                    },
                }
            }
        },
    )
    with pytest.raises(ConfigError, match="dimensionless"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_access_derivation_modbus(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        points={
            "ro": {
                "point_groups": ["g"],
                "address": {"register_type": "input", "address": 100},
                "data_type": "float32",
            },
            "rw": {
                "point_groups": ["g"],
                "address": {"register_type": "holding", "address": 200},
                "data_type": "float32",
            },
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    table = config.point_tables[PointTableId("tab")]
    assert table.points["ro"].access is PointAccess.READ
    assert table.points["rw"].access is PointAccess.READ_WRITE


def test_access_derivation_iec104(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        protocol="iec104",
        points={
            "meas": {
                "point_groups": ["g"],
                "address": {"ioa": 100, "type": "M_ME_NC_1"},
                "data_type": "float32",
            },
            "cmd": {
                "point_groups": ["g"],
                "address": {"ioa": 200, "type": "C_SC_NA_1"},
                "data_type": "bool",
            },
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    table = config.point_tables[PointTableId("tab")]
    assert table.points["meas"].access is PointAccess.READ
    assert table.points["cmd"].access is PointAccess.READ_WRITE


def test_ads_connection_options_preserved(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        protocol="ads",
        points={
            "p1": {
                "point_groups": ["g"],
                "address": {"symbol": "MAIN.n", "data_type": "REAL"},
                "data_type": "REAL",
            }
        },
        protocol_options={"target_net_id": "1.2.3.4.5.6"},
        devices={
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "10.0.0.1", "port": 851},
            }
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    options = config.protocol_options_for(DeviceId("dev1"))
    assert "read_mode" not in options
    assert options["target_net_id"] == "1.2.3.4.5.6"


def test_business_point_identity_is_point_key(tmp_path):
    """统一 Schema 下业务点身份即 Point Key，不再支持显式 business_point_id。"""
    config_dir = write_minimal_config_tree(
        tmp_path,
        point_tables={
            "tab": {
                "protocol": "modbus",
                "points": {
                    "p1": {
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 1},
                        "data_type": "float32",
                    }
                },
            },
            "tab_b": {
                "protocol": "modbus",
                "points": {
                    "p1": {
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 2},
                        "data_type": "float32",
                    }
                },
            },
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    bp_ids = {str(bp) for bp in config.business_points}
    assert bp_ids == {"p1"}


def test_same_point_key_conflicting_data_type_rejected(tmp_path):
    """同名 Point Key 跨点表类型冲突时，按业务点不兼容拒绝。"""
    config_dir = write_minimal_config_tree(
        tmp_path,
        point_tables={
            "tab": {
                "protocol": "modbus",
                "points": {
                    "p1": {
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 1},
                        "data_type": "float32",
                    }
                },
            },
            "tab_b": {
                "protocol": "modbus",
                "points": {
                    "p1": {
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 2},
                        "data_type": "int16",
                    }
                },
            },
        },
    )
    with pytest.raises(ConfigError, match="incompatible business point"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_disabled_devices_excluded_from_snapshot(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices={
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            },
            "dev2": {
                "model_id": "mod",
                "endpoint": {"host": "10.0.0.2", "port": 502},
                "enabled": False,
            },
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    assert set(config.devices) == {DeviceId("dev1")}
    assert config.disabled_devices == frozenset({DeviceId("dev2")})


def test_device_group_membership_loaded(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices={
            "dev1": {
                "model_id": "mod",
                "device_group_ids": ["wind_turbines"],
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    device = config.devices[DeviceId("dev1")]
    assert [str(g) for g in device.device_group_ids] == ["wind_turbines"]
    assert "wind_turbines" in {str(g) for g in config.device_groups}


def test_undeclared_device_group_rejected(tmp_path):
    """DeviceGroup 必须显式声明，引用未声明分组应拒绝。"""
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "devices.yaml",
        {
            "devices": {
                "dev1": {
                    "model_id": "mod",
                    "device_group_ids": ["ghost"],
                    "endpoint": {"host": "10.0.0.1", "port": 502},
                }
            },
            "device_groups": {},
        },
    )
    with pytest.raises(ConfigError, match="unknown device group"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_duplicate_yaml_key_rejected(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    (config_dir / "devices.yaml").write_text(
        "devices:\n  dev1:\n  dev1:\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_point_fields_loaded(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        points={
            "p1": {
                "description": "有功功率",
                "point_groups": ["telemetry", "fast"],
                "address": {"register_type": "holding", "address": 100},
                "data_type": "float32",
            }
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    point = config.point_tables[PointTableId("tab")].points["p1"]
    assert point.description == "有功功率"
    assert point.point_groups == ("telemetry", "fast")


def test_unsupported_protocol_rejected(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    write_yaml(
        config_dir,
        "points.yaml",
        {
            "point_tables": {
                "tab": {
                    "protocol": "opcua",
                    "points": {
                        "p1": {
                            "point_groups": ["g"],
                            "address": {"node": "x"},
                            "data_type": "float32",
                        }
                    },
                }
            }
        },
    )
    with pytest.raises(ConfigError, match="protocol"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_config_dir_digest_stable_and_history_skipped(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    first = config_dir_digest(config_dir)
    assert first == config_dir_digest(config_dir)
    history = config_dir / ".history"
    history.mkdir()
    write_yaml(history, "points.yaml", {"point_tables": {}})
    assert config_dir_digest(config_dir) == first


def test_ads_local_identity_loaded(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        ads={"local_ams_net_id": "5.6.7.8.1.1", "local_ip": "192.168.1.10"},
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    assert config.ads_local is not None
    assert config.ads_local.local_ams_net_id == "5.6.7.8.1.1"
    assert config.ads_local.local_ip == "192.168.1.10"


def test_invalid_ams_net_id_rejected(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path,
        ads={"local_ams_net_id": "not-a-net-id", "local_ip": "192.168.1.10"},
    )
    with pytest.raises(ConfigError, match="AMS Net ID"):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_commander_does_not_consume_tasks_or_sinks(tmp_path):
    """Commander 不消费 tasks/sinks（Collector 关注点），但共享 Core 快照校验。

    合法的 tasks/sinks 正常加载且不进入 CommanderConfig；非法内容由共享
    loader 拒绝——配置错误必须快速失败，而不是被某个进程静默忽略。
    """
    config_dir = write_minimal_config_tree(tmp_path)
    config = load_commander_config(YamlConfigAdapter(config_dir))
    assert config.devices
    assert not hasattr(config, "tasks")
    assert not hasattr(config, "sinks")

    write_yaml(config_dir, "tasks.yaml", {"tasks": {"bad": {}}})
    with pytest.raises(ConfigError):
        load_commander_config(YamlConfigAdapter(config_dir))


def test_watchdog_timing_loaded(tmp_path):
    config_dir = write_minimal_config_tree(
        tmp_path, runtime={"watchdog_interval": 2.0, "health_timeout": 0.5}
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    assert config.watchdog_interval == 2.0
    assert config.health_timeout == 0.5


def test_watchdog_timing_defaults_and_validation(tmp_path):
    config_dir = write_minimal_config_tree(tmp_path)
    config = load_commander_config(YamlConfigAdapter(config_dir))
    assert config.watchdog_interval == 5.0
    assert config.health_timeout == 1.0

    bad_dir = write_minimal_config_tree(tmp_path / "bad", runtime={"health_timeout": 0})
    with pytest.raises(ConfigError):
        load_commander_config(YamlConfigAdapter(bad_dir))


def test_commander_rejects_duplicate_ads_targets(tmp_path) -> None:
    """Commander must enforce the same target Net ID constraint as Collector."""
    from dataclasses import replace

    config_dir = write_minimal_config_tree(
        tmp_path,
        protocol="ads",
        protocol_options={"target_net_id": "192.0.2.10.1.1"},
        points={
            "p1": {
                "point_groups": ["g"],
                "address": {"symbol": "MAIN.x", "data_type": "REAL"},
                "data_type": "REAL",
            }
        },
    )
    config = load_commander_config(YamlConfigAdapter(config_dir))
    original = config.devices[DeviceId("dev1")]
    duplicate = replace(original, device_id=DeviceId("dev2"))

    with pytest.raises(ConfigError, match="shared by devices"):
        replace(config, devices={**config.devices, DeviceId("dev2"): duplicate})
