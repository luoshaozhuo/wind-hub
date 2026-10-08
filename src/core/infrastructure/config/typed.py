"""按主题返回已验证的类型化配置对象。

此层不解析跨文件业务引用，也不管理进程运行时状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from copy import deepcopy
from typing import Any, Mapping

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


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """设备型号和设备实例的独立 Schema 校验结果。"""

    models: DeviceModelsFile
    instances: DeviceInstancesFile


@dataclass(frozen=True, slots=True)
class PointConfig:
    """完成继承展开的点表集合。"""

    tables: Mapping[str, ResolvedTable]


@dataclass(frozen=True, slots=True)
class TaskConfig:
    """仅保证 Task 自身的规则；引用有效性由调用方负责。"""

    definition: TasksFile


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """系统配置的不可变顶层分区；各进程自行解释业务字段。"""

    sections: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class UnitConfig:
    """经 Schema 校验的单位配置。"""

    definition: UnitsFile


class YamlTypedConfigAdapter(YamlConfigReader):
    """复用基础 YAML 读取器，向调用方提供类型化配置。"""

    @staticmethod
    def _validate(model: type, raw: Mapping[str, Any], name: str) -> Any:
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
