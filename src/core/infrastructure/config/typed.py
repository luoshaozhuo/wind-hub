"""按主题返回已验证的类型化配置对象。

此层不解析跨文件业务引用，也不管理进程运行时状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from core.application.config_types import DeviceConfig, PointConfig, SystemConfig, TaskConfig, UnitConfig
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import BaseModel

from core.application import ConfigError
from core.infrastructure.config.point_tables import ResolvedTable, resolve_point_tables
from core.infrastructure.config.raw import (
    DeviceInstancesFile,
    DeviceModelsFile,
    PointTablesFile,
    TasksFile,
    UnitsFile,
)
from core.infrastructure.config.yaml import YamlConfigReader


_ModelT = TypeVar("_ModelT", bound=BaseModel)


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
        sections = {name: deepcopy(value) for name, value in raw.items()}
        return SystemConfig(sections=MappingProxyType(sections))

    def read_device_config(self) -> DeviceConfig:
        return DeviceConfig(
            models=self._validate(DeviceModelsFile, self.read_device_models(), "device_models"),
            instances=self._validate(DeviceInstancesFile, self.read_devices(), "devices"),
        )

    def read_point_config(self) -> PointConfig:
        definition = self._validate(PointTablesFile, self.read_points(), "points")
        tables = resolve_point_tables(definition.point_tables)
        return PointConfig(tables=MappingProxyType(dict(tables)))

    def read_task_config(self) -> TaskConfig:
        return TaskConfig(definition=self._validate(TasksFile, self.read_tasks(), "tasks"))

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
