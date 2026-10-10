"""新 Collector 配置加载（Infrastructure config adapter）单元测试。"""

from __future__ import annotations

import pytest

from collector.infrastructure.config.loader import load_collector_config
from core.application import ConfigError
from core.domain import DeviceId, PointTableId
from core.infrastructure.config import fingerprint_config_set
from tests.support.new_collector import write_collector_config_tree
from tests.support.new_commander import write_yaml


def test_load_minimal_config(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    config = load_collector_config(config_dir)

    device = config.devices[DeviceId("dev1")]
    assert device.endpoint.port == 502
    assert set(config.tasks) == {"t1"}
    task = config.tasks["t1"]
    assert task.interval == 1.0
    assert task.targets == ("s1",)
    assert set(config.sinks) == {"s1"}
    point = config.sinks["s1"].points[0]
    assert point.ref == "dev1.p1"  # 缺省 ref 合成
    assert point.source_data_type == "float32"
    assert point.source_unit == "none"
    assert config.runtime.queue_maxsize == 1000
    assert config.runtime.backpressure_policy == "drop_old"
    assert config.ads_subscribe_devices == frozenset()


def test_runtime_params_parsed(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        runtime={
            "queue_maxsize": 50,
            "backpressure_policy": "drop_new",
            "shutdown_timeout": 3.0,
            "connect_timeout": 2.0,
            "read_timeout": 1.5,
            "write_timeout": 4.0,  # Commander 子集，Collector 容忍并忽略
        },
    )
    config = load_collector_config(config_dir)
    assert config.runtime.queue_maxsize == 50
    assert config.runtime.backpressure_policy == "drop_new"
    assert config.runtime.shutdown_timeout == 3.0
    assert config.runtime.read_timeout == 1.5


def test_ads_local_identity_loaded(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        ads={"local_ams_net_id": "5.6.7.8.1.1", "local_ip": "192.168.1.10"},
    )
    config = load_collector_config(config_dir)
    assert config.ads_local is not None
    assert config.ads_local.local_ams_net_id == "5.6.7.8.1.1"
    assert config.ads_local.local_ip == "192.168.1.10"


def test_ads_local_identity_absent_by_default(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    config = load_collector_config(config_dir)
    assert config.ads_local is None


def test_ads_local_identity_must_be_mapping(tmp_path):
    config_dir = write_collector_config_tree(tmp_path, ads="not-a-mapping")
    with pytest.raises(ConfigError, match="ads"):
        load_collector_config(config_dir)


def test_invalid_backpressure_policy_rejected(tmp_path):
    config_dir = write_collector_config_tree(tmp_path, runtime={"backpressure_policy": "explode"})
    with pytest.raises(ConfigError, match="backpressure_policy"):
        load_collector_config(config_dir)


def test_unknown_runtime_key_rejected(tmp_path):
    config_dir = write_collector_config_tree(tmp_path, runtime={"bogus": 1})
    with pytest.raises(ConfigError, match="unknown keys"):
        load_collector_config(config_dir)


def test_ads_subscribe_enabled_extracted_and_stripped(tmp_path):
    """subscribe_enabled 不进入 protocol_options_by_device，记入 ads_subscribe_devices。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        protocol="ads",
        address={"symbol": "MAIN.n", "data_type": "float32"},
        connection_defaults={"target_net_id": "1.2.3.4.5.6"},
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {
                    "host": "10.0.0.1",
                    "port": 851,
                    "extensions": {"subscribe_enabled": True},
                },
            }
        ],
    )
    config = load_collector_config(config_dir)
    assert config.ads_subscribe_devices == frozenset({DeviceId("dev1")})
    options = config.protocol_options_for(DeviceId("dev1"))
    assert "subscribe_enabled" not in options
    assert options["target_net_id"] == "1.2.3.4.5.6"
    assert options["read_mode"] == "sum"
    view = config.device_view(DeviceId("dev1"))
    assert view.subscribe_enabled is True


def test_disabled_device_excluded_but_task_reference_allowed(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "10.0.0.1", "port": 502},
                "enabled": False,
            }
        ],
    )
    config = load_collector_config(config_dir)
    assert config.devices == {}
    assert config.disabled_devices == frozenset({DeviceId("dev1")})
    # 引用 disabled 设备的 Task 合法（不命中任何实例，与旧行为一致）
    assert "t1" in config.tasks


def test_task_unknown_sink_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "device": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "targets": [{"sink": "ghost"}],
            }
        ],
    )
    with pytest.raises(ConfigError, match="unknown sink"):
        load_collector_config(config_dir)


def test_task_unknown_device_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "device": "ghost",
                "point_group": "g",
                "interval": 1.0,
                "targets": [{"sink": "s1"}],
            }
        ],
    )
    with pytest.raises(ConfigError, match="unknown device"):
        load_collector_config(config_dir)


def test_task_device_group_expansion_and_empty_group_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "device_group": "wind",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        ],
        tasks=[
            {
                "task_id": "tg",
                "device_group": "wind",
                "point_group": "g",
                "interval": 1.0,
                "targets": [{"sink": "s1"}],
            }
        ],
    )
    config = load_collector_config(config_dir)
    assert config.tasks["tg"].device_group == "wind"

    write_yaml(
        config_dir,
        "tasks.yaml",
        {
            "tasks": [
                {
                    "task_id": "tg",
                    "device_group": "solar",
                    "point_group": "g",
                    "interval": 1.0,
                    "targets": [{"sink": "s1"}],
                }
            ]
        },
    )
    with pytest.raises(ConfigError, match="matches no device"):
        load_collector_config(config_dir)


def test_task_point_group_must_exist(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "device": "dev1",
                "point_group": "ghost",
                "interval": 1.0,
                "targets": [{"sink": "s1"}],
            }
        ],
    )
    with pytest.raises(ConfigError, match="point_group"):
        load_collector_config(config_dir)


def test_ads_sequential_device_rejects_scheduled_task(tmp_path):
    config_dir = write_collector_config_tree(
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
    with pytest.raises(ConfigError, match="do not support scheduled collection"):
        load_collector_config(config_dir)


def test_interval_required_except_pure_iec104(tmp_path):
    # modbus 任务缺 interval → 配置错误
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "device": "dev1",
                "point_group": "g",
                "targets": [{"sink": "s1"}],
            }
        ],
    )
    with pytest.raises(ConfigError, match="interval is required"):
        load_collector_config(config_dir)

    # 纯 iec104 订阅任务可省略 interval
    iec_dir = write_collector_config_tree(
        tmp_path / "iec",
        protocol="iec104",
        address={"ioa": 100, "type": "M_ME_NC_1"},
        tasks=[
            {
                "task_id": "t1",
                "device": "dev1",
                "point_group": "g",
                "targets": [{"sink": "s1"}],
            }
        ],
    )
    config = load_collector_config(iec_dir)
    assert config.tasks["t1"].interval is None


def test_sink_scale_offset_on_numeric_source(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks=[
            {
                "name": "s1",
                "type": "file",
                "connection": {"path": str(tmp_path / "o.jsonl")},
                "points": [
                    {
                        "source": {"device_id": "dev1", "point_id": "p1"},
                        "ref": "scada.p1",
                        "scale": 2.0,
                        "offset": 1.0,
                        "address": {"field": "value"},
                    }
                ],
            }
        ],
    )
    config = load_collector_config(config_dir)
    point = config.sinks["s1"].points[0]
    assert point.ref == "scada.p1"
    assert point.scale == 2.0


def test_sink_unknown_point_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks=[
            {
                "name": "s1",
                "type": "file",
                "connection": {"path": str(tmp_path / "o.jsonl")},
                "points": [
                    {
                        "source": {"device_id": "dev1", "point_id": "ghost"},
                        "address": {"field": "value"},
                    }
                ],
            }
        ],
    )
    with pytest.raises(ConfigError, match="unknown point"):
        load_collector_config(config_dir)


def test_sink_scale_on_bool_source_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        points=[
            {
                "point_id": "p1",
                "point_groups": ["g"],
                "address": {"register_type": "coil", "address": 1},
                "data_type": "bool",
            }
        ],
        sinks=[
            {
                "name": "s1",
                "type": "file",
                "connection": {"path": str(tmp_path / "o.jsonl")},
                "points": [
                    {
                        "source": {"device_id": "dev1", "point_id": "p1"},
                        "scale": 2.0,
                        "address": {"field": "value"},
                    }
                ],
            }
        ],
    )
    with pytest.raises(ConfigError, match="numeric source"):
        load_collector_config(config_dir)


def test_modbus_sink_layout_overlap_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks=[
            {
                "name": "s1",
                "type": "modbus",
                "connection": {"host": "127.0.0.1", "port": 15020},
                "points": [
                    {
                        "source": {"device_id": "dev1", "point_id": "p1"},
                        "address": {
                            "unit_id": 1,
                            "register_type": "holding",
                            "address": 100,
                        },
                    },
                    {
                        "source": {"device_id": "dev1", "point_id": "p1"},
                        "ref": "dup",
                        "address": {
                            "unit_id": 1,
                            "register_type": "holding",
                            "address": 101,  # float32 占 100..101 → 重叠
                        },
                    },
                ],
            }
        ],
    )
    with pytest.raises(ConfigError, match="overlap"):
        load_collector_config(config_dir)


def test_iec104_sink_type_id_datatype_mismatch_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks=[
            {
                "name": "s1",
                "type": "iec104",
                "connection": {"host": "127.0.0.1", "port": 12404},
                "points": [
                    {
                        "source": {"device_id": "dev1", "point_id": "p1"},
                        "address": {"ioa": 1001, "type_id": "M_SP_NA_1"},  # 需 bool
                    }
                ],
            }
        ],
    )
    with pytest.raises(ConfigError, match="requires datatype 'bool'"):
        load_collector_config(config_dir)


def test_disabled_sink_not_a_valid_task_target(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks=[
            {
                "name": "s1",
                "type": "file",
                "enabled": False,
                "connection": {"path": str(tmp_path / "o.jsonl")},
                "points": [],
            }
        ],
    )
    with pytest.raises(ConfigError, match="unknown sink"):
        load_collector_config(config_dir)


def test_fingerprint_stable(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    first = fingerprint_config_set(config_dir)
    assert first == fingerprint_config_set(config_dir)
    history = config_dir / ".history"
    history.mkdir()
    write_yaml(history, "points.yaml", {"point_tables": {}})
    assert fingerprint_config_set(config_dir) == first


def test_meta_and_point_groups_loaded(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        points=[
            {
                "point_id": "p1",
                "variable_name": "风速",
                "point_groups": ["g", "fast"],
                "address": {"register_type": "holding", "address": 100},
                "data_type": "float32",
            }
        ],
    )
    config = load_collector_config(config_dir)
    meta = config.meta_for(PointTableId("tab"), "p1")
    assert meta.variable_name == "风速"
    assert meta.point_groups == ("g", "fast")


def test_unknown_protocol_option_is_rejected_at_load(tmp_path):
    """协议参数合法性在加载阶段校验，不推迟到 Driver 构造。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        connection_defaults={"port": 502, "bogus_option": 1},
    )
    with pytest.raises(ConfigError, match="unknown Modbus options"):
        load_collector_config(config_dir)


def test_invalid_protocol_option_value_is_rejected_at_load(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        connection_defaults={"port": 502, "timeout": -1},
    )
    with pytest.raises(ConfigError):
        load_collector_config(config_dir)


def test_read_retries_and_retry_interval_parsed(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        runtime={"read_retries": -1, "retry_interval": 0.25},
    )
    config = load_collector_config(config_dir)
    assert config.runtime.read_retries == -1
    assert config.runtime.retry_interval == 0.25


def test_read_retries_defaults(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    config = load_collector_config(config_dir)
    assert config.runtime.read_retries == 1
    assert config.runtime.retry_interval == 1.0
