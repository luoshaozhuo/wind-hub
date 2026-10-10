"""配置 VO diff 单元测试：按业务 ID 粒度的语义差异。

差异比较由 Config VO 自己决定粒度：设备按 device_id、任务按 task_id、
点表按表名整表比较、型号按 ``device_models.<id>`` / ``device_types.<id>``、
SystemConfig 按共享段、SinksConfig 按 sink name。
"""

from __future__ import annotations

import pytest

from core.application.sink_config import SinksConfig
from core.domain.config import (
    ConfigDiff,
    DeviceInstanceConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    EndpointConfig,
    PointConfig,
    PointTableConfig,
    PointTablesConfig,
    RuntimeSettings,
    SystemConfig,
    TaskConfig,
    TasksConfig,
    UnitDefinitionConfig,
    UnitsConfig,
    ValueChange,
    diff_mapping,
)


def _endpoint(port: int | None = 502) -> EndpointConfig:
    return EndpointConfig(host="127.0.0.1", port=port, extensions={})


def _device(port: int | None = 502, **overrides) -> DeviceInstanceConfig:
    data = {
        "device_id": "dev1",
        "model": "mod",
        "device_group": None,
        "endpoint": _endpoint(port),
        "enabled": True,
    }
    return DeviceInstanceConfig(**(data | overrides))


def _devices(*devices: DeviceInstanceConfig) -> DevicesConfig:
    return DevicesConfig(devices={d.device_id: d for d in devices})


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
        device_types={"turbine": None},
        device_models={"mod": _model(protocol)},
    )


def _point(scale: float = 1.0) -> PointConfig:
    return PointConfig(
        point_id="p1",
        variable_name=None,
        point_groups=("g",),
        address={"address": 100},
        data_type="float32",
        scale=scale,
        offset=0.0,
        unit="none",
        description=None,
    )


def _tables(scale: float = 1.0) -> PointTablesConfig:
    return PointTablesConfig(
        tables={"tab": PointTableConfig(protocol="modbus", points={"p1": _point(scale)})}
    )


def _tasks(interval: float = 1.0) -> TasksConfig:
    return TasksConfig(
        tasks={
            "t1": TaskConfig(
                task_id="t1",
                point_group="g",
                targets=("s1",),
                device="dev1",
                interval=interval,
            )
        }
    )


def _sinks(*names: str) -> SinksConfig:
    return SinksConfig(
        sinks=[
            {
                "name": name,
                "type": "file",
                "connection": {"path": f"/tmp/{name}.csv"},
                "points": [],
            }
            for name in names or ("s1",)
        ]
    )


# ---------------------------------------------------------------------------
# diff_mapping 共享函数
# ---------------------------------------------------------------------------


def test_diff_mapping_added_removed_changed():
    result = diff_mapping({"a": 1, "b": 2}, {"b": 3, "c": 4})
    assert result.added == {"c": 4}
    assert result.removed == {"a": 1}
    assert result.changed == {"b": ValueChange(old=2, new=3)}
    assert result.has_any_changes


def test_diff_mapping_strict_scalar_semantics():
    # 类型与值都相等才算相同（1 与 1.0 视为不同配置值）。
    result = diff_mapping({"a": 1}, {"a": 1.0})
    assert result.changed == {"a": ValueChange(old=1, new=1.0)}


def test_config_diff_result_is_immutable():
    result = diff_mapping({}, {"a": 1})
    with pytest.raises(TypeError):
        result.added["x"] = 1  # type: ignore[index]


# ---------------------------------------------------------------------------
# DevicesConfig.diff——按 device_id
# ---------------------------------------------------------------------------


def test_devices_diff_identical():
    config = _devices(_device())
    assert not config.diff(config).has_any_changes


def test_devices_diff_changed_whole_object():
    result = _devices(_device(port=502)).diff(_devices(_device(port=503)))
    assert result.added == {}
    assert result.removed == {}
    assert set(result.changed) == {"dev1"}
    change = result.changed["dev1"]
    assert isinstance(change.old, DeviceInstanceConfig)
    assert change.old.endpoint.port == 502
    assert change.new.endpoint.port == 503


def test_devices_diff_added_removed():
    new_device = _device(device_id="dev2")
    result = _devices(_device()).diff(_devices(_device(), new_device))
    assert set(result.added) == {"dev2"}
    assert result.added["dev2"] == new_device

    result = _devices(_device(), new_device).diff(_devices(_device()))
    assert set(result.removed) == {"dev2"}


def test_devices_diff_rejects_other_type():
    with pytest.raises(TypeError):
        _devices().diff(_models())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DeviceModelsConfig.diff——device_models / device_types 前缀
# ---------------------------------------------------------------------------


def test_models_diff_changed_model_and_type():
    old = _models("modbus")
    new = DeviceModelsConfig(
        device_types={"turbine": "风机"},
        device_models={"mod": _model("iec104")},
    )
    result = old.diff(new)
    assert set(result.changed) == {"device_types.turbine", "device_models.mod"}
    assert result.changed["device_models.mod"].old == _model("modbus")
    assert result.changed["device_models.mod"].new == _model("iec104")


def test_models_diff_added_removed():
    result = DeviceModelsConfig(device_types={}, device_models={}).diff(_models())
    assert set(result.added) == {"device_types.turbine", "device_models.mod"}
    result = _models().diff(DeviceModelsConfig(device_types={}, device_models={}))
    assert set(result.removed) == {"device_types.turbine", "device_models.mod"}


# ---------------------------------------------------------------------------
# PointTablesConfig.diff——整表粒度
# ---------------------------------------------------------------------------


def test_point_tables_diff_point_change_is_whole_table_change():
    result = _tables(1.0).diff(_tables(2.0))
    assert set(result.changed) == {"tab"}
    change = result.changed["tab"]
    assert isinstance(change.old, PointTableConfig)
    assert change.old.points["p1"].scale == 1.0
    assert change.new.points["p1"].scale == 2.0


def test_point_tables_diff_added_removed():
    result = _tables().diff(PointTablesConfig(tables={}))
    assert set(result.removed) == {"tab"}


# ---------------------------------------------------------------------------
# TasksConfig.diff——按 task_id
# ---------------------------------------------------------------------------


def test_tasks_diff_changed_whole_object():
    result = _tasks(1.0).diff(_tasks(5.0))
    assert set(result.changed) == {"t1"}
    assert result.changed["t1"].old.interval == 1.0  # type: ignore[union-attr]
    assert result.changed["t1"].new.interval == 5.0  # type: ignore[union-attr]


def test_tasks_diff_added_removed():
    result = _tasks().diff(TasksConfig(tasks={}))
    assert set(result.removed) == {"t1"}


# ---------------------------------------------------------------------------
# SystemConfig.diff——按共享段
# ---------------------------------------------------------------------------


def test_system_diff_by_section():
    old = SystemConfig(runtime=RuntimeSettings(queue_maxsize=64))
    new = SystemConfig(runtime=RuntimeSettings(queue_maxsize=128))
    result = old.diff(new)
    assert set(result.changed) == {"runtime"}
    assert result.changed["runtime"] == ValueChange(old=old.runtime, new=new.runtime)


def test_system_diff_site_fields():
    old = SystemConfig()
    new = SystemConfig(site_id="s1", site_name="现场")
    result = old.diff(new)
    assert result.added == {}
    assert set(result.changed) == {"site_id", "site_name"}


def test_system_diff_ads_added_removed():
    from core.domain.config import ADSLocalConfig

    ads = ADSLocalConfig(
        local_ams_net_id="1.2.3.4.5.6", local_ip="127.0.0.1", username="u", password=""
    )
    result = SystemConfig().diff(SystemConfig(ads=ads))
    assert result.changed["ads"] == ValueChange(old=None, new=ads)


# ---------------------------------------------------------------------------
# UnitsConfig.diff / SinksConfig.diff
# ---------------------------------------------------------------------------


def test_units_diff():
    old = UnitsConfig(units={"a": UnitDefinitionConfig("A", None)})
    new = UnitsConfig(units={"a": UnitDefinitionConfig("B", None)})
    result = old.diff(new)
    assert set(result.changed) == {"a"}
    assert not old.diff(old).has_any_changes


def test_sinks_diff_by_name():
    result = _sinks("s1").diff(_sinks("s1", "s2"))
    assert set(result.added) == {"s2"}
    assert result.changed == {}

    result = _sinks("s1", "s2").diff(_sinks("s1"))
    assert set(result.removed) == {"s2"}


def test_sinks_diff_changed_whole_object():
    old = _sinks()
    new = SinksConfig(
        sinks=[
            {
                "name": "s1",
                "type": "file",
                "enabled": False,
                "connection": {"path": "/tmp/s1.csv"},
                "points": [],
            }
        ]
    )
    result = old.diff(new)
    assert set(result.changed) == {"s1"}
    assert result.changed["s1"].old.enabled is True  # type: ignore[union-attr]
    assert result.changed["s1"].new.enabled is False  # type: ignore[union-attr]


def test_sinks_diff_rejects_other_type():
    with pytest.raises(TypeError):
        _sinks().diff(_units_default())  # type: ignore[arg-type]


def _units_default() -> UnitsConfig:
    return UnitsConfig(units={"none": UnitDefinitionConfig("", None)})


def test_diff_is_deterministic():
    old = _devices(_device(port=502))
    new = _devices(_device(port=503))
    first = old.diff(new)
    second = old.diff(new)
    assert first == second
    assert isinstance(first, ConfigDiff)
