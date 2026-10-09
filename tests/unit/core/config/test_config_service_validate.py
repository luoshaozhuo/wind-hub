"""ConfigService.validate 单主题与跨主题校验测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError, ConfigService
from core.application.port import ConfigTopic
from core.domain.config import (
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
)


class _NullPort:
    """validate 不访问 Port——read/save 不应被调用。"""

    def read(self, topic):  # pragma: no cover
        raise AssertionError("validate must not read")

    def save(self, topic, config):  # pragma: no cover
        raise AssertionError("validate must not save")


@pytest.fixture
def service():
    return ConfigService(_NullPort())


def _models(point_table: str = "tab", protocol: str = "modbus") -> DeviceModelsConfig:
    return DeviceModelsConfig(
        device_types={"turbine": DeviceTypeConfig(name=None)},
        device_models={
            "mod": DeviceModelConfig(
                device_type="turbine",
                manufacturer=None,
                model=None,
                protocol=protocol,
                point_table=point_table,
                read_mode=None,
                properties={},
                connection_defaults={},
            )
        },
    )


def _devices(model: str = "mod") -> DevicesConfig:
    return DevicesConfig(
        devices=(
            DeviceInstanceConfig(
                device_id="dev1",
                model=model,
                device_group="wind",
                endpoint=EndpointConfig(host="127.0.0.1", port=502, extensions={}),
                enabled=True,
            ),
        )
    )


def _points(unit: str = "none", protocol: str = "modbus") -> PointTablesConfig:
    return PointTablesConfig(
        tables={
            "tab": PointTableConfig(
                protocol=protocol,
                points={
                    "p1": PointConfig(
                        point_id="p1",
                        variable_name=None,
                        point_groups=("g",),
                        address={"register_type": "holding", "address": 100},
                        data_type="float32",
                        scale=1.0,
                        offset=0.0,
                        unit=unit,
                        description=None,
                    )
                },
            )
        }
    )


def _units() -> UnitsConfig:
    return UnitsConfig(units={"none": UnitDefinitionConfig(symbol="", name=None)})


# ---------------------------------------------------------------------------
# 单主题校验
# ---------------------------------------------------------------------------


def test_validate_single_topic_without_related(service):
    service.validate(ConfigTopic.DEVICE_MODELS, _models())
    service.validate(ConfigTopic.DEVICES, _devices(model="anything"))
    service.validate(ConfigTopic.POINTS, _points(unit="anything"))
    service.validate(ConfigTopic.UNITS, _units())


def test_validate_rejects_topic_type_mismatch(service):
    with pytest.raises(ConfigError, match="expects config of type"):
        service.validate(ConfigTopic.DEVICES, _units())


# ---------------------------------------------------------------------------
# 跨主题校验：缺失 related 明确报错，不误报通过
# ---------------------------------------------------------------------------


def test_validate_reports_missing_related_topics(service):
    with pytest.raises(ConfigError, match="cannot fully validate"):
        service.validate(ConfigTopic.DEVICES, _devices(), related={})
    with pytest.raises(ConfigError, match="cannot fully validate"):
        service.validate(ConfigTopic.TASKS, TasksConfig(tasks=()), related={})


def test_validate_device_models_against_points(service):
    service.validate(
        ConfigTopic.DEVICE_MODELS,
        _models(),
        related={ConfigTopic.POINTS: _points()},
    )
    with pytest.raises(ConfigError, match="unknown point_table"):
        service.validate(
            ConfigTopic.DEVICE_MODELS,
            _models(point_table="ghost"),
            related={ConfigTopic.POINTS: _points()},
        )
    with pytest.raises(ConfigError, match="does not match"):
        service.validate(
            ConfigTopic.DEVICE_MODELS,
            _models(protocol="iec104"),
            related={ConfigTopic.POINTS: _points()},
        )


def test_validate_devices_against_models(service):
    service.validate(
        ConfigTopic.DEVICES,
        _devices(),
        related={ConfigTopic.DEVICE_MODELS: _models()},
    )
    with pytest.raises(ConfigError, match="unknown model"):
        service.validate(
            ConfigTopic.DEVICES,
            _devices(model="ghost"),
            related={ConfigTopic.DEVICE_MODELS: _models()},
        )


def test_validate_points_against_units(service):
    service.validate(
        ConfigTopic.POINTS, _points(), related={ConfigTopic.UNITS: _units()}
    )
    with pytest.raises(ConfigError, match="unknown unit"):
        service.validate(
            ConfigTopic.POINTS,
            _points(unit="m/s"),
            related={ConfigTopic.UNITS: _units()},
        )


def test_validate_tasks_cross_references(service):
    related = {
        ConfigTopic.DEVICE_MODELS: _models(),
        ConfigTopic.DEVICES: _devices(),
        ConfigTopic.POINTS: _points(),
        ConfigTopic.SINKS: _sinks(),
    }
    ok = TasksConfig(
        tasks=(TaskConfig(task_id="t", point_group="g", targets=("s1",), device="dev1"),)
    )
    service.validate(ConfigTopic.TASKS, ok, related=related)

    bad_device = TasksConfig(
        tasks=(TaskConfig(task_id="t", point_group="g", targets=("s1",), device="ghost"),)
    )
    with pytest.raises(ConfigError, match="unknown device"):
        service.validate(ConfigTopic.TASKS, bad_device, related=related)

    bad_group = TasksConfig(
        tasks=(
            TaskConfig(task_id="t", point_group="g", targets=("s1",), device_group="ghost"),
        )
    )
    with pytest.raises(ConfigError, match="unknown device_group"):
        service.validate(ConfigTopic.TASKS, bad_group, related=related)

    bad_point_group = TasksConfig(
        tasks=(
            TaskConfig(task_id="t", point_group="nope", targets=("s1",), device="dev1"),
        )
    )
    with pytest.raises(ConfigError, match="matches no point"):
        service.validate(ConfigTopic.TASKS, bad_point_group, related=related)

    bad_sink = TasksConfig(
        tasks=(TaskConfig(task_id="t", point_group="g", targets=("ghost",), device="dev1"),)
    )
    with pytest.raises(ConfigError, match="unknown sink"):
        service.validate(ConfigTopic.TASKS, bad_sink, related=related)


def _sink_point(device_id: str = "dev1", point_id: str = "p1", unit: str | None = None):
    from core.application.sink_config import SinkPoint, SinkSource, StreamSinkAddress

    return SinkPoint(
        source=SinkSource(device_id=device_id, point_id=point_id),
        unit=unit,
        address=StreamSinkAddress(field="value"),
    )


def _sinks(**point_kwargs):
    from core.application.sink_config import SinkConfig, SinksConfig

    return SinksConfig(
        sinks=[
            SinkConfig(
                name="s1",
                type="file",
                connection={"path": "/tmp/out.jsonl"},
                points=[_sink_point(**point_kwargs)],
            )
        ]
    )


def test_validate_sinks_cross_references(service):
    related = {
        ConfigTopic.DEVICE_MODELS: _models(),
        ConfigTopic.DEVICES: _devices(),
        ConfigTopic.POINTS: _points(),
        ConfigTopic.UNITS: _units(),
    }
    service.validate(ConfigTopic.SINKS, _sinks(), related=related)

    with pytest.raises(ConfigError, match="unknown device"):
        service.validate(ConfigTopic.SINKS, _sinks(device_id="ghost"), related=related)

    with pytest.raises(ConfigError, match="unknown point"):
        service.validate(ConfigTopic.SINKS, _sinks(point_id="ghost"), related=related)

    with pytest.raises(ConfigError, match="unknown unit"):
        service.validate(ConfigTopic.SINKS, _sinks(unit="m/s"), related=related)
