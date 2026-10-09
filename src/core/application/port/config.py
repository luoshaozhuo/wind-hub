"""配置读取端口：按主题按需读取类型化配置与版本指纹。

调用方只读取自身需要的主题；读取点表不会隐式加载无关的 Sink/Task
配置。公开契约只暴露类型化配置对象，不暴露无约束 ``dict[str, Any]``。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from core.application.config_types import (
    ConfigTopic,
    DeviceConfig,
    DeviceInstancesConfig,
    DeviceModelsConfig,
    PointTablesConfig,
    SystemConfig,
    TasksConfig,
    UnitConfig,
)
from core.application.sink_config import SinksConfig


@runtime_checkable
class ConfigReader(Protocol):
    """按需读取配置主题的端口；具体 YAML/文件实现属于 Adapter。"""

    def read_system_config(self) -> SystemConfig: ...

    def read_device_models_config(self) -> DeviceModelsConfig: ...

    def read_device_instances_config(self) -> DeviceInstancesConfig: ...

    def read_device_config(self) -> DeviceConfig: ...

    def read_point_tables_config(self) -> PointTablesConfig: ...

    def read_tasks_config(self) -> TasksConfig: ...

    def read_sink_config(self) -> SinksConfig: ...

    def read_unit_config(self) -> UnitConfig: ...

    def fingerprint(self) -> str:
        """整个配置目录的稳定指纹（跨进程配置版本契约）。"""
        ...

    def fingerprint_topics(self, topics: Iterable[ConfigTopic]) -> str:
        """仅覆盖指定主题文件的指纹；用于进程内部的版本一致性检查。"""
        ...

    def open_snapshot(self, topics: Iterable[ConfigTopic]) -> ConfigSnapshot:
        """开启只覆盖指定主题的一致性读取会话。"""
        ...


@runtime_checkable
class ConfigSnapshot(ConfigReader, Protocol):
    """一次配置读取会话：会话内多次读取解析自同一批捕获内容。

    - 会话内不混用不同版本：读取一律来自 open 时捕获的内容；
    - 依赖范围显式：读取未声明主题立即报错，不会隐式读盘；
    - 外部并发修改由 :meth:`verify_unchanged` 检测，失败即中止，
      不输出混合版本配置。快照不宣称跨文件原子：捕获仍是逐文件
      顺序读取，verify 用于兜底检测捕获窗口内的写入。
    """

    def verify_unchanged(self) -> None:
        """校验声明主题自 open 以来未被外部修改；被修改则抛 ConfigError。"""
        ...


__all__ = ["ConfigReader", "ConfigSnapshot", "ConfigTopic"]
