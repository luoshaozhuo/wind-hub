"""配置领域 VO 单元测试：构造不变量、不可变性、相等性。

VO 只依赖 Domain 层，不感知 YAML/文件；不变量违反抛 ValueError。
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from core.domain.config import (
    DeviceInstanceConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    EndpointConfig,
    PointConfig,
    PointTableConfig,
    PointTablesConfig,
    RuntimeSettings,
    TaskConfig,
    TasksConfig,
    UnitDefinitionConfig,
    UnitsConfig,
)


def _point(**overrides) -> PointConfig:
    data = {
        "point_id": "p1",
        "variable_name": None,
        "point_groups": ("g",),
        "address": {"register_type": "holding", "address": 100},
        "data_type": "float32",
        "scale": 1.0,
        "offset": 0.0,
        "unit": "none",
        "description": None,
    }
    return PointConfig(**(data | overrides))


def _model(**overrides) -> DeviceModelConfig:
    data = {
        "device_type": "turbine",
        "manufacturer": None,
        "model": None,
        "protocol": "modbus",
        "point_table": "tab",
        "read_mode": None,
        "properties": {},
        "connection_defaults": {},
    }
    return DeviceModelConfig(**(data | overrides))


def _device(device_id: str = "dev1") -> DeviceInstanceConfig:
    return DeviceInstanceConfig(
        device_id=device_id,
        model="mod",
        device_group=None,
        endpoint=EndpointConfig(host="127.0.0.1", port=502, extensions={}),
        enabled=True,
    )


def _task(task_id: str = "t1") -> TaskConfig:
    return TaskConfig(task_id=task_id, point_group="g", targets=("s1",), device="dev1")


# ---------------------------------------------------------------------------
# 构造不变量
# ---------------------------------------------------------------------------


def test_device_model_rejects_unknown_protocol():
    with pytest.raises(ValueError, match="protocol"):
        _model(protocol="opcua")


def test_device_model_read_mode_is_ads_only():
    assert _model(protocol="ads", read_mode="sum").read_mode == "sum"
    with pytest.raises(ValueError, match="ADS-specific"):
        _model(protocol="modbus", read_mode="sum")
    with pytest.raises(ValueError, match="read_mode"):
        _model(protocol="ads", read_mode="poll")


def test_endpoint_port_must_be_int_not_bool():
    with pytest.raises(ValueError, match="port"):
        EndpointConfig(host="h", port=True, extensions={})
    assert EndpointConfig(host="h", port=None, extensions={}).port is None


def test_devices_config_key_must_match_device_id():
    with pytest.raises(ValueError, match="does not match device_id"):
        DevicesConfig(devices={"other": _device("d1")})


def test_point_rejects_bad_groups_and_data_type():
    with pytest.raises(ValueError, match="point_groups must be non-empty"):
        _point(point_groups=())
    with pytest.raises(ValueError, match="duplicate point_groups"):
        _point(point_groups=("g", "g"))
    with pytest.raises(ValueError, match="data_type"):
        _point(data_type="not-a-type")


def test_point_rejects_non_finite_or_bool_numbers():
    with pytest.raises(ValueError, match="scale"):
        _point(scale=float("nan"))
    with pytest.raises(ValueError, match="offset"):
        _point(offset=float("inf"))
    with pytest.raises(ValueError, match="scale"):
        _point(scale=True)


def test_point_table_key_must_match_point_id():
    with pytest.raises(ValueError, match="does not match"):
        PointTableConfig(protocol="modbus", points={"other": _point()})


def test_point_table_rejects_unknown_protocol():
    with pytest.raises(ValueError, match="protocol"):
        PointTableConfig(protocol="opcua", points={})


def test_task_requires_exactly_one_of_device_and_group():
    with pytest.raises(ValueError, match="XOR"):
        TaskConfig(task_id="t", point_group="g", targets=("s",))
    with pytest.raises(ValueError, match="XOR"):
        TaskConfig(
            task_id="t", point_group="g", targets=("s",), device="d", device_group="grp"
        )


def test_task_validates_interval_targets_enabled():
    with pytest.raises(ValueError, match="interval"):
        TaskConfig(task_id="t", point_group="g", targets=("s",), device="d", interval=0)
    with pytest.raises(ValueError, match="targets must be non-empty"):
        TaskConfig(task_id="t", point_group="g", targets=(), device="d")
    with pytest.raises(ValueError, match="duplicate target"):
        TaskConfig(task_id="t", point_group="g", targets=("s", "s"), device="d")
    with pytest.raises(ValueError, match="enabled"):
        TaskConfig(task_id="t", point_group="g", targets=("s",), device="d", enabled=1)


def test_tasks_config_key_must_match_task_id():
    with pytest.raises(ValueError, match="does not match task_id"):
        TasksConfig(tasks={"other": _task("t")})


def test_runtime_settings_invariants():
    with pytest.raises(ValueError, match="queue_maxsize"):
        RuntimeSettings(queue_maxsize=0)
    with pytest.raises(ValueError, match="queue_maxsize"):
        RuntimeSettings(queue_maxsize=True)
    with pytest.raises(ValueError, match="backpressure_policy"):
        RuntimeSettings(backpressure_policy="explode")
    with pytest.raises(ValueError, match="connect_timeout"):
        RuntimeSettings(connect_timeout=-1.0)
    assert RuntimeSettings(connect_timeout=1).connect_timeout == 1.0


# ---------------------------------------------------------------------------
# 不可变性
# ---------------------------------------------------------------------------


def test_vos_are_frozen():
    point = _point()
    with pytest.raises(AttributeError):
        point.scale = 2.0  # type: ignore[misc]
    devices = DevicesConfig(devices={"dev1": _device()})
    with pytest.raises(AttributeError):
        devices.devices = {}  # type: ignore[misc]


def test_collections_are_defensively_frozen():
    address = {"register_type": "holding", "address": 100}
    point = _point(address=address)
    address["address"] = 999  # 构造后修改源不影响 VO
    assert point.address["address"] == 100
    with pytest.raises(TypeError):
        point.address["address"] = 1  # type: ignore[index]

    assert isinstance(point.address, MappingProxyType)
    assert isinstance(point.point_groups, tuple)

    model = _model(properties={"a": [1, 2]})
    assert isinstance(model.properties, MappingProxyType)
    assert model.properties["a"] == (1, 2)
    with pytest.raises(TypeError):
        model.properties["a"] = ()  # type: ignore[index]

    models = DeviceModelsConfig(
        device_types={"turbine": None}, device_models={"m": _model()}
    )
    with pytest.raises(TypeError):
        models.device_models["x"] = _model()  # type: ignore[index]

    tables = PointTablesConfig(tables={"tab": PointTableConfig(protocol="modbus", points={})})
    with pytest.raises(TypeError):
        tables.tables["x"] = None  # type: ignore[index]

    units = UnitsConfig(units={"none": UnitDefinitionConfig(symbol="", name=None)})
    with pytest.raises(TypeError):
        units.units["x"] = None  # type: ignore[index]

    tasks = TasksConfig(tasks={"t1": _task()})
    assert isinstance(tasks.tasks, MappingProxyType)
    with pytest.raises(TypeError):
        tasks.tasks["x"] = None  # type: ignore[index]

    devices_map = DevicesConfig(devices={"dev1": _device()})
    assert isinstance(devices_map.devices, MappingProxyType)


# ---------------------------------------------------------------------------
# 相等性
# ---------------------------------------------------------------------------


def test_vo_equality_by_value():
    assert _point() == _point()
    assert _point() != _point(scale=2.0)
    assert DevicesConfig(devices={"dev1": _device()}) == DevicesConfig(
        devices={"dev1": _device()}
    )
    assert _task() == _task()
    assert RuntimeSettings(queue_maxsize=10) == RuntimeSettings(queue_maxsize=10)
