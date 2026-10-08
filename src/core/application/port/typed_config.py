"""类型化配置读取端口：业务模块按需获取独立主题。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.infrastructure.config.typed import (
        DeviceConfig,
        PointConfig,
        TaskConfig,
        UnitConfig,
    )


class TypedConfigReader(Protocol):
    """只约束读取能力；具体 YAML/文件实现属于 Adapter。"""

    def read_device_config(self) -> DeviceConfig: ...

    def read_point_config(self) -> PointConfig: ...

    def read_task_config(self) -> TaskConfig: ...

    def read_unit_config(self) -> UnitConfig: ...
