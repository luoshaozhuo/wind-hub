"""DiffConfigUseCase 语义比较规则测试。

路径格式（spec §5）：``devices.WTG001.endpoint.host``、
``points.TABLE_A.points.WindSpeed.address``、``tasks.TASK_A.interval``、
``device_models.MODEL_A.protocol``。
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

from core.application import DiffConfigUseCase
from core.application.port import ConfigTopic
from core.domain.config import (
    ConfigDiff,
    DeviceInstanceConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    DeviceTypeConfig,
    EndpointConfig,
    PointConfig,
    PointTableConfig,
    PointTablesConfig,
    TaskConfig,
    TasksConfig,
    UnitDefinitionConfig,
    UnitsConfig,
    ValueChange,
)


@pytest.fixture
def diff() -> DiffConfigUseCase:
    return DiffConfigUseCase()


def _endpoint(port: int = 502) -> EndpointConfig:
    return EndpointConfig(host="127.0.0.1", port=port, extensions={})


def _device(device_id: str = "dev1", *, port: int = 502, enabled: bool = True):
    return DeviceInstanceConfig(
        device_id=device_id,
        model="mod",
        device_group=None,
        endpoint=_endpoint(port),
        enabled=enabled,
    )


def _devices(*devices: DeviceInstanceConfig) -> DevicesConfig:
    return DevicesConfig(devices=tuple(devices))


def _model(protocol: str = "modbus") -> DeviceModelConfig:
    return DeviceModelConfig(
        device_type="turbine",
        manufacturer=None,
        model=None,
        protocol=protocol,
        point_table="tab",
        read_mode=None,
        properties={},
        connection_defaults={},
    )


def _models(protocol: str = "modbus") -> DeviceModelsConfig:
    return DeviceModelsConfig(
        device_types={"turbine": DeviceTypeConfig(name=None)},
        device_models={"mod": _model(protocol)},
    )


def _point(address: int = 100) -> PointConfig:
    return PointConfig(
        point_id="p1",
        variable_name=None,
        point_groups=("g",),
        address={"register_type": "holding", "address": address},
        data_type="float32",
        scale=1.0,
        offset=0.0,
        unit="none",
        description=None,
    )


def _tables(address: int = 100) -> PointTablesConfig:
    return PointTablesConfig(
        tables={"tab": PointTableConfig(protocol="modbus", points={"p1": _point(address)})}
    )


def _task(task_id: str = "t1", interval: float = 1.0) -> TaskConfig:
    return TaskConfig(
        task_id=task_id, point_group="g", targets=("s1",), device="dev1", interval=interval
    )


# ---------------------------------------------------------------------------
# 基本规则：无变化 / 类型校验
# ---------------------------------------------------------------------------


def test_identical_configs_have_no_changes(diff):
    for topic, config in (
        (ConfigTopic.DEVICES, _devices(_device())),
        (ConfigTopic.DEVICE_MODELS, _models()),
        (ConfigTopic.POINTS, _tables()),
        (ConfigTopic.TASKS, TasksConfig(tasks=(_task(),))),
        (ConfigTopic.UNITS, UnitsConfig(units={"none": UnitDefinitionConfig("", None)})),
    ):
        result = diff.execute(topic, config, config)
        assert not result.has_any_changes
        assert result.added == {} and result.removed == {} and result.modified == {}


def test_execute_rejects_type_mismatch(diff):
    with pytest.raises(TypeError, match="old"):
        diff.execute(ConfigTopic.DEVICES, _models(), _devices(_device()))
    with pytest.raises(TypeError, match="new"):
        diff.execute(ConfigTopic.DEVICES, _devices(_device()), _models())


# ---------------------------------------------------------------------------
# modified 定位到实际变化字段
# ---------------------------------------------------------------------------


def test_device_endpoint_change_located_at_leaf(diff):
    result = diff.execute(
        ConfigTopic.DEVICES, _devices(_device(port=502)), _devices(_device(port=503))
    )
    assert result.modified == {
        "devices.dev1.endpoint.port": ValueChange(old=502, new=503)
    }
    assert result.added == {} and result.removed == {}


def test_point_address_change_path_matches_spec(diff):
    result = diff.execute(ConfigTopic.POINTS, _tables(100), _tables(200))
    assert set(result.modified) == {"points.tab.points.p1.address.address"}
    change = result.modified["points.tab.points.p1.address.address"]
    assert change == ValueChange(old=100, new=200)


def test_task_interval_change_path_matches_spec(diff):
    old = TasksConfig(tasks=(_task(interval=1.0),))
    new = TasksConfig(tasks=(_task(interval=5.0),))
    result = diff.execute(ConfigTopic.TASKS, old, new)
    assert set(result.modified) == {"tasks.t1.interval"}
    assert result.modified["tasks.t1.interval"] == ValueChange(old=1.0, new=5.0)


def test_device_model_protocol_change_path_matches_spec(diff):
    result = diff.execute(ConfigTopic.DEVICE_MODELS, _models("modbus"), _models("iec104"))
    assert set(result.modified) == {"device_models.mod.protocol"}


# ---------------------------------------------------------------------------
# added/removed 整体记录于对象路径，不展开叶子
# ---------------------------------------------------------------------------


def test_added_device_recorded_whole_at_object_path(diff):
    new_device = _device("dev2")
    result = diff.execute(
        ConfigTopic.DEVICES, _devices(_device()), _devices(_device(), new_device)
    )
    assert set(result.added) == {"devices.dev2"}
    assert result.added["devices.dev2"] == new_device
    assert result.removed == {} and result.modified == {}


def test_removed_task_recorded_whole_at_object_path(diff):
    removed = _task("t2")
    old = TasksConfig(tasks=(_task(), removed))
    new = TasksConfig(tasks=(_task(),))
    result = diff.execute(ConfigTopic.TASKS, old, new)
    assert set(result.removed) == {"tasks.t2"}
    assert result.removed["tasks.t2"] == removed


def test_added_point_recorded_whole(diff):
    old = _tables()
    new_table = PointTableConfig(
        protocol="modbus",
        points={"p1": _point(), "p2": replace(_point(), point_id="p2")},
    )
    new = PointTablesConfig(tables={"tab": new_table})
    result = diff.execute(ConfigTopic.POINTS, old, new)
    assert set(result.added) == {"points.tab.points.p2"}
    assert result.added["points.tab.points.p2"] == new_table.points["p2"]


# ---------------------------------------------------------------------------
# 比较语义：Mapping 顺序无关 / ID 键集合 / 严格标量
# ---------------------------------------------------------------------------


def test_mapping_order_is_irrelevant(diff):
    a = UnitsConfig(
        units={"a": UnitDefinitionConfig("A", None), "b": UnitDefinitionConfig("B", None)}
    )
    b = UnitsConfig(
        units={"b": UnitDefinitionConfig("B", None), "a": UnitDefinitionConfig("A", None)}
    )
    assert not diff.execute(ConfigTopic.UNITS, a, b).has_any_changes


def test_id_keyed_collection_ignores_list_position(diff):
    a = _devices(_device("d1"), _device("d2", port=503))
    b = _devices(_device("d2", port=503), _device("d1"))
    assert not diff.execute(ConfigTopic.DEVICES, a, b).has_any_changes

    c = _devices(_device("d1"), _device("d2", port=504))
    result = diff.execute(ConfigTopic.DEVICES, a, c)
    assert set(result.modified) == {"devices.d2.endpoint.port"}


def test_strict_scalar_equality_no_float_tolerance(diff):
    old = TasksConfig(tasks=(_task(interval=1.0),))
    new = TasksConfig(tasks=(_task(interval=1.0 + 1e-12),))
    result = diff.execute(ConfigTopic.TASKS, old, new)
    assert set(result.modified) == {"tasks.t1.interval"}


def test_scalar_type_change_is_modified(diff):
    old = _devices(_device(port=502))
    new_device = replace(
        _device(), endpoint=EndpointConfig(host="127.0.0.1", port=None, extensions={})
    )
    result = diff.execute(ConfigTopic.DEVICES, old, _devices(new_device))
    assert result.modified["devices.dev1.endpoint.port"] == ValueChange(old=502, new=None)


def test_sequence_length_mismatch_recorded_at_field(diff):
    old_point = _point()
    new_point = replace(_point(), point_groups=("g", "g2"))
    old = PointTablesConfig(
        tables={"tab": PointTableConfig(protocol="modbus", points={"p1": old_point})}
    )
    new = PointTablesConfig(
        tables={"tab": PointTableConfig(protocol="modbus", points={"p1": new_point})}
    )
    result = diff.execute(ConfigTopic.POINTS, old, new)
    assert set(result.modified) == {"points.tab.points.p1.point_groups"}
    change = result.modified["points.tab.points.p1.point_groups"]
    assert change.old == ("g",) and change.new == ("g", "g2")


def test_device_type_changes_under_device_types_key(diff):
    old = _models()
    new = DeviceModelsConfig(
        device_types={"turbine": DeviceTypeConfig(name="风机")},
        device_models={"mod": _model()},
    )
    result = diff.execute(ConfigTopic.DEVICE_MODELS, old, new)
    assert set(result.modified) == {"device_models.device_types.turbine.name"}


# ---------------------------------------------------------------------------
# 结果不可变 / 输入不被修改 / 确定性
# ---------------------------------------------------------------------------


def test_result_is_immutable(diff):
    result = diff.execute(ConfigTopic.DEVICES, _devices(_device()), _devices(_device(port=1)))
    assert isinstance(result, ConfigDiff)
    assert isinstance(result.modified, MappingProxyType)
    with pytest.raises(TypeError):
        result.modified["x"] = None  # type: ignore[index]
    with pytest.raises(AttributeError):
        result.topic = ConfigTopic.TASKS  # type: ignore[misc]


def test_diff_does_not_mutate_inputs(diff):
    old = _devices(_device())
    new = _devices(_device(port=503), _device("dev2"))
    diff.execute(ConfigTopic.DEVICES, old, new)
    assert old == _devices(_device())
    assert new == _devices(_device(port=503), _device("dev2"))


def test_diff_is_deterministic(diff):
    old = _devices(_device())
    new = _devices(_device(port=503), _device("dev2"), _device("dev3"))
    first = diff.execute(ConfigTopic.DEVICES, old, new)
    second = diff.execute(ConfigTopic.DEVICES, old, new)
    assert first == second


def test_recorded_mapping_values_are_frozen(diff):
    old = _tables(100)
    removed_table = diff.execute(
        ConfigTopic.POINTS, old, PointTablesConfig(tables={})
    ).removed["points.tab"]
    assert isinstance(removed_table, PointTableConfig)
    with pytest.raises(TypeError):
        removed_table.points["x"] = None  # type: ignore[index]
