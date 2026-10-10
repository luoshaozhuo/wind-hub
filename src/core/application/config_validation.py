"""跨主题配置引用校验：普通函数，无 Service 层。

单主题结构与字段不变量由配置 VO 构造保证；本模块只承担单 VO 无法
判断的跨主题引用检查（设备→型号→点表、任务→设备/分组/Sink 等）。
校验纯内存进行——不读取 YAML、不访问文件系统，调用方显式提供被检
配置及其关联主题配置。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

from core.application.errors import ConfigError
from core.application.port.config import TOPIC_CONFIG_TYPES, ConfigValue
from core.application.sink_config import SinksConfig
from core.domain.config import (
    ConfigTopic,
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    TasksConfig,
    UnitsConfig,
)

#: 完整跨主题校验各主题所需的关联主题。``related`` 提供时若缺少必需
#: 主题，明确报告无法完整校验，而不是默默宣称全部校验通过。
_REQUIRED_RELATED: dict[ConfigTopic, frozenset[ConfigTopic]] = {
    ConfigTopic.SYSTEM: frozenset(),
    ConfigTopic.UNITS: frozenset(),
    ConfigTopic.DEVICE_MODELS: frozenset({ConfigTopic.POINTS}),
    ConfigTopic.POINTS: frozenset({ConfigTopic.UNITS}),
    ConfigTopic.DEVICES: frozenset({ConfigTopic.DEVICE_MODELS}),
    ConfigTopic.TASKS: frozenset({
        ConfigTopic.DEVICE_MODELS,
        ConfigTopic.DEVICES,
        ConfigTopic.POINTS,
        ConfigTopic.SINKS,
    }),
    ConfigTopic.SINKS: frozenset({
        ConfigTopic.DEVICE_MODELS,
        ConfigTopic.DEVICES,
        ConfigTopic.POINTS,
        ConfigTopic.UNITS,
    }),
}


def validate_config(
    topic: ConfigTopic,
    config: ConfigValue,
    *,
    related: Mapping[ConfigTopic, ConfigValue] | None = None,
) -> None:
    """校验配置：topic 类型匹配 + （提供 related 时）跨主题引用检查。

    单主题结构与字段不变量由配置 VO 构造保证，此处复核 topic 与
    config 类型匹配。``related`` 提供时执行相关主题的跨文件引用
    检查；缺少完整校验必需的关联主题时抛 ConfigError 明确报告，
    不默默宣称全部校验通过。不会隐式读取未提供的关联配置。
    """
    expected = TOPIC_CONFIG_TYPES[topic]
    if not isinstance(config, expected):
        raise ConfigError(
            f"topic '{topic}' expects config of type {expected.__name__}, "
            f"got {type(config).__name__}"
        )
    if related is None:
        return
    available = set(related) | {topic}
    missing = sorted(str(t) for t in _REQUIRED_RELATED[topic] if t not in available)
    if missing:
        raise ConfigError(
            f"cannot fully validate '{topic}': missing related topics {missing}"
        )
    merged: dict[ConfigTopic, ConfigValue] = {**related, topic: config}
    if topic is ConfigTopic.DEVICE_MODELS:
        _validate_models_against_points(
            _as(topic, config, DeviceModelsConfig),
            _as(ConfigTopic.POINTS, merged[ConfigTopic.POINTS], PointTablesConfig),
        )
    elif topic is ConfigTopic.DEVICES:
        _validate_devices_against_models(
            _as(topic, config, DevicesConfig),
            _as(
                ConfigTopic.DEVICE_MODELS,
                merged[ConfigTopic.DEVICE_MODELS],
                DeviceModelsConfig,
            ),
        )
    elif topic is ConfigTopic.POINTS:
        _validate_points_against_units(
            _as(topic, config, PointTablesConfig),
            _as(ConfigTopic.UNITS, merged[ConfigTopic.UNITS], UnitsConfig),
        )
    elif topic is ConfigTopic.TASKS:
        _validate_tasks(
            _as(topic, config, TasksConfig),
            models=_as(
                ConfigTopic.DEVICE_MODELS, merged[ConfigTopic.DEVICE_MODELS], DeviceModelsConfig
            ),
            devices=_as(ConfigTopic.DEVICES, merged[ConfigTopic.DEVICES], DevicesConfig),
            points=_as(ConfigTopic.POINTS, merged[ConfigTopic.POINTS], PointTablesConfig),
            sinks=_as(ConfigTopic.SINKS, merged[ConfigTopic.SINKS], SinksConfig),
        )
    elif topic is ConfigTopic.SINKS:
        _validate_sinks(
            _as(topic, config, SinksConfig),
            models=_as(
                ConfigTopic.DEVICE_MODELS, merged[ConfigTopic.DEVICE_MODELS], DeviceModelsConfig
            ),
            devices=_as(ConfigTopic.DEVICES, merged[ConfigTopic.DEVICES], DevicesConfig),
            points=_as(ConfigTopic.POINTS, merged[ConfigTopic.POINTS], PointTablesConfig),
            units=_as(ConfigTopic.UNITS, merged[ConfigTopic.UNITS], UnitsConfig),
        )


_T = TypeVar("_T")


def _as(topic: ConfigTopic, config: ConfigValue, expected: type[_T]) -> _T:
    if not isinstance(config, expected):
        raise ConfigError(
            f"related topic '{topic}' expects config of type {expected.__name__}, "
            f"got {type(config).__name__}"
        )
    return config


def _validate_models_against_points(
    models: DeviceModelsConfig,
    points: PointTablesConfig,
) -> None:
    for model_id, model in models.device_models.items():
        table = points.tables.get(model.point_table)
        if table is None:
            raise ConfigError(
                f"Device model '{model_id}' references unknown point_table "
                f"'{model.point_table}'"
            )
        if table.protocol != model.protocol:
            raise ConfigError(
                f"Device model '{model_id}' protocol '{model.protocol}' does not "
                f"match point table '{model.point_table}' protocol '{table.protocol}'"
            )
        if model.device_type not in models.device_types:
            raise ConfigError(
                f"Device model '{model_id}' references unknown device_type "
                f"'{model.device_type}' (available: {sorted(models.device_types)})"
            )


def _validate_devices_against_models(
    devices: DevicesConfig,
    models: DeviceModelsConfig,
) -> None:
    for device in devices.devices:
        if device.model not in models.device_models:
            raise ConfigError(
                f"Device '{device.device_id}' references unknown model '{device.model}' "
                f"(available: {sorted(models.device_models)})"
            )


def _validate_points_against_units(
    points: PointTablesConfig,
    units: UnitsConfig,
) -> None:
    for table_name, table in points.tables.items():
        for point in table.points.values():
            if point.unit not in units.units:
                raise ConfigError(
                    f"Point table '{table_name}' point '{point.point_id}' references "
                    f"unknown unit '{point.unit}' (not defined in units.yaml)"
                )


def _point_group_known(
    point_group: str,
    table_name: str,
    points: PointTablesConfig,
) -> bool:
    table = points.tables.get(table_name)
    if table is None:
        return False
    return any(point_group in point.point_groups for point in table.points.values())


def _validate_tasks(
    tasks: TasksConfig,
    *,
    models: DeviceModelsConfig,
    devices: DevicesConfig,
    points: PointTablesConfig,
    sinks: SinksConfig,
) -> None:
    device_ids = {device.device_id for device in devices.devices}
    # device_group 匹配包含 disabled 设备的分组（与运行时行为一致）。
    groups = {
        device.device_group for device in devices.devices if device.device_group is not None
    }
    sink_names = {sink.name for sink in sinks.sinks}
    table_by_device = {
        device.device_id: models.device_models[device.model].point_table
        for device in devices.devices
        if device.model in models.device_models
    }
    # 组内每台参与采集（enabled）设备的点表全集——运行时
    # validate_task_targets 要求 point_group 在每台命中 enabled 设备
    # 的绑定点表中存在；只保留同组最后一张表会漏检并依赖声明顺序。
    group_tables: dict[str, set[str]] = {}
    for device in devices.devices:
        if device.device_group is None or not device.enabled:
            continue
        table_name = table_by_device.get(device.device_id)
        if table_name is None:
            continue
        group_tables.setdefault(device.device_group, set()).add(table_name)
    for task in tasks.tasks:
        if task.device is not None:
            if task.device not in device_ids:
                raise ConfigError(
                    f"Task '{task.task_id}' references unknown device '{task.device}'"
                )
            table_name = table_by_device.get(task.device)
            if table_name is not None and not _point_group_known(
                task.point_group, table_name, points
            ):
                raise ConfigError(
                    f"Task '{task.task_id}': point_group '{task.point_group}' matches "
                    f"no point in table '{table_name}' of device '{task.device}'"
                )
        else:
            assert task.device_group is not None
            if task.device_group not in groups:
                raise ConfigError(
                    f"Task '{task.task_id}' references unknown device_group "
                    f"'{task.device_group}'"
                )
            missing_tables = sorted(
                table_name
                for table_name in group_tables.get(task.device_group, set())
                if not _point_group_known(task.point_group, table_name, points)
            )
            if missing_tables:
                raise ConfigError(
                    f"Task '{task.task_id}': point_group '{task.point_group}' matches "
                    f"no point in tables {missing_tables} of group "
                    f"'{task.device_group}'"
                )
        for target in task.targets:
            if target not in sink_names:
                raise ConfigError(
                    f"Task '{task.task_id}' references unknown sink '{target}'"
                )


def _validate_sinks(
    sinks: SinksConfig,
    *,
    models: DeviceModelsConfig,
    devices: DevicesConfig,
    points: PointTablesConfig,
    units: UnitsConfig,
) -> None:
    table_by_device: dict[str, str] = {}
    for device in devices.devices:
        model = models.device_models.get(device.model)
        if model is not None:
            table_by_device[device.device_id] = model.point_table
    for sink in sinks.sinks:
        for point in sink.points:
            device_id = point.source.device_id
            table_name = table_by_device.get(device_id)
            if table_name is None:
                raise ConfigError(
                    f"Sink '{sink.name}' point '{point.source.point_id}' references "
                    f"unknown device '{device_id}'"
                )
            table = points.tables.get(table_name)
            if table is None or point.source.point_id not in table.points:
                raise ConfigError(
                    f"Sink '{sink.name}' point '{point.source.point_id}' references "
                    f"unknown point on device '{device_id}'"
                )
            if point.unit is not None and point.unit not in units.units:
                raise ConfigError(
                    f"Sink '{sink.name}' point '{point.source.point_id}' references "
                    f"unknown unit '{point.unit}'"
                )


__all__ = ["validate_config"]
