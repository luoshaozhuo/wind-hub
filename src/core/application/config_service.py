"""统一配置服务：按主题组织 read / save / validate。

ConfigService 只依赖 :class:`ConfigPort`，不直接 import YAML Reader、
不访问文件系统；读取返回不可变配置 VO，保存先验证再写入（原子性由
Port 实现保证），校验分单主题约束与显式跨主题引用检查。

本服务不承担运行时配置激活职责——``save()`` 只持久化配置，热更新流程
不经过本方法。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeVar

from core.application.errors import ConfigError
from core.application.port.config import (
    TOPIC_CONFIG_TYPES,
    ConfigPort,
    ConfigSnapshot,
    ConfigSnapshotPort,
    ConfigValue,
)
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


class ConfigService:
    """统一配置服务：read / save / validate + 一致性读取会话组合。"""

    def __init__(
        self,
        port: ConfigPort,
        *,
        snapshots: ConfigSnapshotPort | None = None,
    ) -> None:
        self._port = port
        self._snapshots = snapshots

    def read(self, topic: ConfigTopic) -> ConfigValue:
        """读取指定主题，返回不可变配置 VO；失败抛 ConfigError。"""
        return self._port.read(topic)

    def save(self, topic: ConfigTopic, config: ConfigValue) -> None:
        """先验证再写入；topic 与 config 类型必须匹配。

        写入原子性（临时文件 + fsync + 原子替换）由 Port 实现保证；
        本方法只持久化配置，不参与任何运行时配置激活。
        """
        self._check_topic_type(topic, config)
        self.validate(topic, config)
        self._port.save(topic, config)

    def validate(
        self,
        topic: ConfigTopic,
        config: ConfigValue,
        *,
        related: Mapping[ConfigTopic, ConfigValue] | None = None,
    ) -> None:
        """校验配置：单主题约束 + （提供 related 时）跨主题引用检查。

        单主题结构与字段不变量由配置 VO 构造保证，此处复核 topic 与
        config 类型匹配。``related`` 提供时执行相关主题的跨文件引用
        检查；缺少完整校验必需的关联主题时抛 ConfigError 明确报告，
        不默默宣称全部校验通过。不会隐式读取未提供的关联配置。
        """
        self._check_topic_type(topic, config)
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

    def open_snapshot(self, topics: Iterable[ConfigTopic]) -> ConfigSnapshot:
        """开启一致性读取会话；端口不支持快照时抛 ConfigError。"""
        if self._snapshots is None:
            raise ConfigError("config snapshots are not supported by this port")
        return self._snapshots.open_snapshot(topics)

    @staticmethod
    def _check_topic_type(topic: ConfigTopic, config: ConfigValue) -> None:
        expected = TOPIC_CONFIG_TYPES[topic]
        if not isinstance(config, expected):
            raise ConfigError(
                f"topic '{topic}' expects config of type {expected.__name__}, "
                f"got {type(config).__name__}"
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
    group_tables = {
        device.device_group: table_by_device[device.device_id]
        for device in devices.devices
        if device.device_group is not None and device.device_id in table_by_device
    }
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
            table_name = group_tables.get(task.device_group)
            if table_name is not None and not _point_group_known(
                task.point_group, table_name, points
            ):
                raise ConfigError(
                    f"Task '{task.task_id}': point_group '{task.point_group}' matches "
                    f"no point in table '{table_name}' of group '{task.device_group}'"
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


__all__ = ["ConfigService"]
