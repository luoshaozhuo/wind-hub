"""全量配置端口：一次读取领域配置快照。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.application.config_snapshot import ConfigSnapshot


@runtime_checkable
class ConfigPort(Protocol):
    """定义完整配置加载契约，不保留按主题读取接口。"""

    def load(self) -> ConfigSnapshot:
        """读取并校验全部配置，返回完整快照。"""
        ...


__all__ = ["ConfigPort"]
