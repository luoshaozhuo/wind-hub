"""新 Collector 配置加载（Infrastructure config adapter）单元测试。"""

from __future__ import annotations

import pytest

from collector.infrastructure.config.loader import load_collector_config
from core.application import ConfigError
from core.domain import DeviceId, PointTableId
from core.infrastructure.config import YamlConfigAdapter, config_dir_digest
from tests.support.new_collector import write_collector_config_tree
from tests.support.new_commander import write_yaml


def test_load_minimal_config(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    config = load_collector_config(YamlConfigAdapter(config_dir))

    device = config.devices[DeviceId("dev1")]
    assert device.endpoint.port == 502
    assert set(config.tasks) == {"t1"}
    task = config.tasks["t1"]
    assert task.interval == 1.0
    assert task.sink_ids == ("s1",)
    assert set(config.sinks) == {"s1"}
    point = config.sinks["s1"].points[0]
    assert point["ref"] == "dev1.p1"  # 缺省 ref 合成
    assert point["source_data_type"] == "float32"
    assert point["source_unit"] == "none"
    assert config.runtime.queue_maxsize == 1000
    assert config.runtime.backpressure_policy == "drop_old"
    assert not any(device.subscribe_enabled for device in config.devices.values())


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
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert config.runtime.queue_maxsize == 50
    assert config.runtime.backpressure_policy == "drop_new"
    assert config.runtime.shutdown_timeout == 3.0
    assert config.runtime.read_timeout == 1.5


def test_ads_local_identity_loaded(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        ads={"local_ams_net_id": "5.6.7.8.1.1", "local_ip": "192.168.1.10"},
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert config.ads_local is not None
    assert config.ads_local.local_ams_net_id == "5.6.7.8.1.1"
    assert config.ads_local.local_ip == "192.168.1.10"


def test_ads_local_identity_absent_by_default(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert config.ads_local is None


def test_ads_local_identity_must_be_mapping(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        ads="not-a-mapping",  # type: ignore[arg-type]  # 故意传入非法类型，验证加载时报错
    )
    with pytest.raises(ConfigError, match="ads"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_invalid_backpressure_policy_rejected(tmp_path):
    config_dir = write_collector_config_tree(tmp_path, runtime={"backpressure_policy": "explode"})
    with pytest.raises(ConfigError, match="backpressure_policy"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_unknown_runtime_key_rejected(tmp_path):
    config_dir = write_collector_config_tree(tmp_path, runtime={"bogus": 1})
    with pytest.raises(ConfigError, match="unknown keys"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_ads_subscribe_enabled_kept_on_device(tmp_path):
    """subscribe_enabled 保存在 Device，不传入 Driver 协议参数。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        protocol="ads",
        points={
            "p1": {
                "point_groups": ["g"],
                "address": {"symbol": "MAIN.n"},
                "data_type": "REAL",
            }
        },
        protocol_options={"target_net_id": "1.2.3.4.5.6"},
        devices={
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "10.0.0.1", "port": 851},
                "protocol_options": {"subscribe_enabled": True},
            }
        },
        sinks={},
        tasks={},
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    device = config.devices[DeviceId("dev1")]
    assert device.subscribe_enabled
    # subscribe_enabled 已从协议参数中剥离，不随 Device.protocol_options 下发
    assert "subscribe_enabled" not in device.protocol_options
    model = config.device_models[device.device_model_id]
    assert model.protocol_options["target_net_id"] == "1.2.3.4.5.6"


def test_sink_reference_to_ads_point_uses_logical_data_type(tmp_path):
    """复现生产缺陷：Sink 引用 ADS 点时应解析为逻辑类型 float32（REAL 的逻辑类型），
    而不是协议原生名 REAL；当前 collector loader 直接取 point.ext['data_type']，
    导致任何映射 ADS 点的 Sink 配置都无法加载。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        protocol="ads",
        points={
            "p1": {
                "point_groups": ["g"],
                "address": {"symbol": "MAIN.power"},
                "data_type": "REAL",
            }
        },
        protocol_options={"target_net_id": "1.2.3.4.5.6"},
        sinks={
            "s1": {
                "type": "redis",
                "connection": {"host": "127.0.0.1"},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [
                    {
                        "sink_id": "s1",
                        "points": [
                            {
                                "source": {"device_id": "dev1", "point_id": "p1"},
                                "address": {"field": "value"},
                            }
                        ],
                    }
                ],
            }
        },
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert config.sinks["s1"].points[0]["source_data_type"] == "float32"


@pytest.mark.parametrize(
    ("protocol", "address", "native_type", "logical_type", "protocol_options"),
    [
        ("ads", {"symbol": "MAIN.flag"}, "BOOL", "bool", {"target_net_id": "1.2.3.4.5.6"}),
        ("ads", {"symbol": "MAIN.code"}, "INT", "int16", {"target_net_id": "1.2.3.4.5.6"}),
        ("ads", {"symbol": "MAIN.count"}, "DINT", "int32", {"target_net_id": "1.2.3.4.5.6"}),
        ("ads", {"symbol": "MAIN.power"}, "REAL", "float32", {"target_net_id": "1.2.3.4.5.6"}),
        ("ads", {"symbol": "MAIN.energy"}, "LREAL", "float64", {"target_net_id": "1.2.3.4.5.6"}),
        (
            "ads",
            {"symbol": "MAIN.serial", "size": 16},
            "STRING",
            "str",
            {"target_net_id": "1.2.3.4.5.6"},
        ),
        # Modbus / IEC104 原生表示与逻辑标量同名，但同样必须以业务点目录的
        # 逻辑类型为 Sink 边界真值，而不是直接透传 point.ext。
        ("modbus", {"register_type": "holding", "address": 100}, "int32", "int32", None),
        ("iec104", {"type": "M_ME_NC_1", "ioa": 1}, "float32", "float32", None),
    ],
)
def test_sink_source_data_type_is_always_logical(
    tmp_path, protocol, address, native_type, logical_type, protocol_options
):
    """Sink 源类型参数化：ADS 各常见原生类型映射到逻辑类型；Modbus/IEC104 同名透传。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        protocol=protocol,
        points={
            "p1": {
                "point_groups": ["g"],
                "address": address,
                "data_type": native_type,
            }
        },
        protocol_options=protocol_options,
        sinks={
            "s1": {
                "type": "redis",
                "connection": {"host": "127.0.0.1"},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [
                    {
                        "sink_id": "s1",
                        "points": [
                            {
                                "source": {"device_id": "dev1", "point_id": "p1"},
                                "address": {"field": "value"},
                            }
                        ],
                    }
                ],
            }
        },
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    resolved = config.sinks["s1"].points[0]
    assert resolved["source_data_type"] == logical_type
    assert resolved["datatype"] == logical_type
    # Driver 侧仍保留协议原生编码信息。
    table = config.point_table_for_device(DeviceId("dev1"))
    assert table.points["p1"].ext["data_type"] == native_type


def test_disabled_device_excluded_but_task_reference_allowed(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        devices={
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "10.0.0.1", "port": 502},
                "enabled": False,
            }
        },
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert config.devices == {}
    assert config.disabled_devices == frozenset({DeviceId("dev1")})
    # 引用 disabled 设备的 Task 合法（不命中任何实例，与旧行为一致）
    assert "t1" in config.tasks


def test_task_unknown_sink_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [{"sink_id": "ghost"}],
            }
        },
    )
    with pytest.raises(ConfigError, match="unknown sink"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_task_unknown_group_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks={
            "t1": {
                "device_group_id": "ghost",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [{"sink_id": "s1"}],
            }
        },
    )
    with pytest.raises(ConfigError, match="unknown device group"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_task_device_group_expansion_and_empty_group_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        devices={
            "dev1": {
                "model_id": "mod",
                "device_group_ids": ["wind"],
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        },
        tasks={
            "tg": {
                "device_group_id": "wind",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [{"sink_id": "s1"}],
            }
        },
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert str(config.tasks["tg"].device_group_id) == "wind"

    write_yaml(
        config_dir,
        "tasks.yaml",
        {
            "tasks": {
                "tg": {
                    "device_group_id": "solar",
                    "point_group": "g",
                    "interval": 1.0,
                    "outputs": [{"sink_id": "s1"}],
                }
            }
        },
    )
    with pytest.raises(ConfigError, match="unknown device group"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_task_point_group_must_exist(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "ghost",
                "interval": 1.0,
                "outputs": [{"sink_id": "s1"}],
            }
        },
    )
    with pytest.raises(ConfigError, match="point group"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_interval_required_for_all_protocols(tmp_path):
    # modbus 任务缺 interval → 配置错误
    config_dir = write_collector_config_tree(
        tmp_path,
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "outputs": [{"sink_id": "s1"}],
            }
        },
    )
    with pytest.raises(ConfigError, match="interval"):
        load_collector_config(YamlConfigAdapter(config_dir))

    # IEC104 同样要求显式给出合法 interval
    iec_dir = write_collector_config_tree(
        tmp_path / "iec",
        protocol="iec104",
        address={"ioa": 100, "type": "M_ME_NC_1"},
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "outputs": [{"sink_id": "s1"}],
            }
        },
    )
    with pytest.raises(ConfigError, match="interval"):
        load_collector_config(YamlConfigAdapter(iec_dir))


def test_sink_scale_offset_on_numeric_source(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks={
            "s1": {
                "type": "redis",
                "connection": {"host": "127.0.0.1"},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [
                    {
                        "sink_id": "s1",
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
            }
        },
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    point = config.sinks["s1"].points[0]
    assert point["ref"] == "scada.p1"
    assert point["scale"] == 2.0


def test_sink_unknown_point_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks={
            "s1": {
                "type": "redis",
                "connection": {"host": "127.0.0.1"},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [
                    {
                        "sink_id": "s1",
                        "points": [
                            {
                                "source": {"device_id": "dev1", "point_id": "ghost"},
                                "address": {"field": "value"},
                            }
                        ],
                    }
                ],
            }
        },
    )
    with pytest.raises(ConfigError, match="unknown point"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_sink_scale_on_bool_source_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        points={
            "p1": {
                "point_groups": ["g"],
                "address": {"register_type": "coil", "address": 1},
                "data_type": "bool",
            }
        },
        sinks={
            "s1": {
                "type": "redis",
                "connection": {"host": "127.0.0.1"},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [
                    {
                        "sink_id": "s1",
                        "points": [
                            {
                                "source": {"device_id": "dev1", "point_id": "p1"},
                                "scale": 2.0,
                                "address": {"field": "value"},
                            }
                        ],
                    }
                ],
            }
        },
    )
    with pytest.raises(ConfigError, match="numeric source"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_modbus_sink_layout_overlap_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks={
            "s1": {
                "type": "modbus",
                "connection": {"host": "127.0.0.1", "port": 15020},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [
                    {
                        "sink_id": "s1",
                        "points": [
                            {
                                "source": {"device_id": "dev1", "point_id": "p1"},
                                "address": {
                                    "slave_id": 1,
                                    "register_type": "holding",
                                    "address": 100,
                                },
                            },
                            {
                                "source": {"device_id": "dev1", "point_id": "p1"},
                                "ref": "dup",
                                "address": {
                                    "slave_id": 1,
                                    "register_type": "holding",
                                    "address": 101,  # float32 占 100..101 → 重叠
                                },
                            },
                        ],
                    }
                ],
            }
        },
    )
    with pytest.raises(ConfigError, match="overlap"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_retired_iec104_sink_type_rejected(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks={
            "s1": {
                "type": "iec104",
                "connection": {"host": "127.0.0.1", "port": 12404},
            }
        },
    )
    with pytest.raises(ConfigError, match="sink type 'iec104' must be one of"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_disabled_sink_not_a_valid_task_target(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks={
            "s1": {
                "type": "redis",
                "enabled": False,
                "connection": {"host": "127.0.0.1"},
            }
        },
    )
    with pytest.raises(ConfigError, match="disabled sink"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_config_dir_digest_stable(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    first = config_dir_digest(config_dir)
    assert first == config_dir_digest(config_dir)
    history = config_dir / ".history"
    history.mkdir()
    write_yaml(history, "points.yaml", {"point_tables": {}})
    assert config_dir_digest(config_dir) == first


def test_meta_and_point_groups_loaded(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        points={
            "p1": {
                "description": "风速",
                "point_groups": ["g", "fast"],
                "address": {"register_type": "holding", "address": 100},
                "data_type": "float32",
            }
        },
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    point = config.point_tables[PointTableId("tab")].points["p1"]
    assert point.description == "风速"
    assert point.point_groups == ("g", "fast")


def test_unknown_protocol_option_is_rejected_at_load(tmp_path):
    """协议参数合法性在加载阶段校验，不推迟到 Driver 构造。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        protocol_options={"bogus_option": 1},
    )
    with pytest.raises(ConfigError, match="invalid protocol options"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_invalid_protocol_option_value_is_rejected_at_load(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        protocol_options={"timeout": -1},
    )
    with pytest.raises(ConfigError, match="invalid protocol options"):
        load_collector_config(YamlConfigAdapter(config_dir))


def test_watchdog_timing_parsed(tmp_path):
    config_dir = write_collector_config_tree(
        tmp_path,
        runtime={"watchdog_interval": 2.5, "health_timeout": 0.25},
    )
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert config.runtime.watchdog_interval == 2.5
    assert config.runtime.health_timeout == 0.25


def test_watchdog_timing_defaults(tmp_path):
    config_dir = write_collector_config_tree(tmp_path)
    config = load_collector_config(YamlConfigAdapter(config_dir))
    assert config.runtime.watchdog_interval == 5.0
    assert config.runtime.health_timeout == 1.0


def test_redis_wildcard_output_expands_to_task_group_sources(tmp_path):
    """空 points 的 Redis 输出 = 任务组全量；loader 物化显式白名单与默认前缀。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks={
            "r1": {
                "type": "redis",
                "connection": {"host": "127.0.0.1", "port": 6379},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [{"sink_id": "r1", "key_prefix": "wf"}],
            }
        },
    )

    config = load_collector_config(YamlConfigAdapter(config_dir))

    sink = config.sinks["r1"]
    assert sink.task_sources == {"t1": (("dev1", "p1"),)}
    assert sink.task_key_prefixes == {"t1": "wf"}


def test_modbus_output_without_explicit_points_rejected(tmp_path):
    """Modbus 从站无法为通配点分配寄存器：空 points 在解析期拒绝。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        sinks={
            "m1": {
                "type": "modbus",
                "connection": {"host": "127.0.0.1", "port": 1502},
            }
        },
        tasks={
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "outputs": [{"sink_id": "m1"}],
            }
        },
    )

    with pytest.raises(ConfigError, match="modbus output"):
        load_collector_config(YamlConfigAdapter(config_dir))
