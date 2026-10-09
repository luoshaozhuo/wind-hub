"""配置读取端口：按主题按需读取类型化配置与版本指纹。

调用方只读取自身需要的主题；读取点表不会隐式加载无关的 Sink/Task
配置。公开契约只暴露类型化配置对象，不暴露无约束 ``dict[str, Any]``。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol, runtime_checkable

from core.application.config_types import (
    DeviceConfig,
    DeviceInstancesConfig,
    DeviceModelsConfig,
    PointConfig,
    SystemConfig,
    TaskConfig,
    UnitConfig,
)
from core.application.sink_config import SinksConfig


class ConfigTopic(StrEnum):
    """配置主题；与磁盘文件名的映射由 Infrastructure Adapter 决定。"""

    SYSTEM = "system"
    DEVICE_MODELS = "device_models"
    DEVICES = "devices"
    POINTS = "points"
    UNITS = "units"
    TASKS = "tasks"
    SINKS = "sinks"


@runtime_checkable
class ConfigReader(Protocol):
    """按需读取配置主题的端口；具体 YAML/文件实现属于 Adapter。"""

    def read_system_config(self) -> SystemConfig: ...

    def read_device_models_config(self) -> DeviceModelsConfig: ...

    def read_device_instances_config(self) -> DeviceInstancesConfig: ...

    def read_device_config(self) -> DeviceConfig: ...

    def read_point_config(self) -> PointConfig: ...

    def read_task_config(self) -> TaskConfig: ...

    def read_sink_config(self) -> SinksConfig: ...

    def read_unit_config(self) -> UnitConfig: ...

    def fingerprint(self) -> str:
        """整个配置目录的稳定指纹（跨进程配置版本契约）。"""
        ...

    def fingerprint_topics(self, topics: Iterable[ConfigTopic]) -> str:
        """仅覆盖指定主题文件的指纹；用于进程内部的版本一致性检查。"""
        ...


__all__ = ["ConfigReader", "ConfigTopic"]
