"""按主题返回已验证的类型化配置对象。

此层不解析跨文件业务引用，也不管理进程运行时状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import BaseModel

from core.application import ConfigError
from core.application.config_types import (
    DeviceConfig,
    DeviceInstancesConfig,
    DeviceModelsConfig,
    PointConfig,
    SystemConfig,
    TaskConfig,
    TaskDefinition,
    UnitConfig,
)
from core.application.sink_config import SinksConfig
from core.infrastructure.config.point_tables import resolve_point_tables
from core.infrastructure.config.raw import (
    DeviceInstancesFile,
    DeviceModelsFile,
    PointTablesFile,
    TasksFile,
    UnitsFile,
)
from core.infrastructure.config.yaml import YamlConfigReader


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _freeze_config(value: Any) -> Any:
    """递归复制并冻结系统配置，阻止对共享配置的原地修改。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_config(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_config(item) for item in value)
    return value


class YamlTypedConfigAdapter(YamlConfigReader):
    """复用基础 YAML 读取器，向调用方提供类型化配置。"""

    @staticmethod
    def _validate(model: type[_ModelT], raw: Mapping[str, Any], name: str) -> _ModelT:
        try:
            return model.model_validate(raw)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"Invalid {name} configuration: {exc}") from exc

    def read_system_config(self) -> SystemConfig:
        raw = self.read_system()
        return SystemConfig(sections=_freeze_config(raw))

    def read_device_models_config(self) -> DeviceModelsConfig:
        definition = self._validate(DeviceModelsFile, self.read_device_models(), "device_models")
        return DeviceModelsConfig(definition=definition)

    def read_device_instances_config(self) -> DeviceInstancesConfig:
        definition = self._validate(DeviceInstancesFile, self.read_devices(), "devices")
        return DeviceInstancesConfig(definition=definition)

    def read_device_config(self) -> DeviceConfig:
        return DeviceConfig(
            models=self.read_device_models_config().definition,
            instances=self.read_device_instances_config().definition,
        )

    def read_point_config(self) -> PointConfig:
        definition = self._validate(PointTablesFile, self.read_points(), "points")
        tables = resolve_point_tables(definition.point_tables)
        return PointConfig(tables=MappingProxyType(dict(tables)))

    def read_task_config(self) -> TaskConfig:
        definition = self._validate(TasksFile, self.read_tasks(), "tasks")
        return TaskConfig(
            tasks=tuple(
                TaskDefinition(
                    task_id=task.task_id,
                    device=task.device,
                    device_group=task.device_group,
                    point_group=task.point_group,
                    interval=task.interval,
                    targets=tuple(target.sink for target in task.targets),
                    enabled=task.enabled,
                )
                for task in definition.tasks
            )
        )

    def read_sink_config(self) -> SinksConfig:
        return self._validate(SinksConfig, self.read_sinks(), "sinks")

    def read_unit_config(self) -> UnitConfig:
        return UnitConfig(definition=self._validate(UnitsFile, self.read_units(), "units"))


__all__ = [
    "DeviceConfig",
    "PointConfig",
    "TaskConfig",
    "SystemConfig",
    "UnitConfig",
    "YamlTypedConfigAdapter",
]
