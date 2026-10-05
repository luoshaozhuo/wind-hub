"""Sink 注册表——Sink 类型名到 SinkPort factory 的映射。

与 :class:`wind_hub_core.protocol.registry.ProtocolRegistry` 同一设计语言：
注册表只保存「类型 → factory」，不持有 Sink 运行实例；内置 Sink 的注册发生在
组合根显式调用的 ``build_sink_registry``（adapter 包内），不依赖 import
side effect。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from wind_hub_core.model.errors import ConfigError

if TYPE_CHECKING:
    from wind_hub_collector.application.port.sink import SinkPort
    from wind_hub_core.config import ResolvedSinkConfig


class SinkRegistry:
    """Sink 类型名到 SinkPort factory 的注册表。"""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[ResolvedSinkConfig], SinkPort]] = {}

    def register(
        self,
        type_name: str,
        factory: Callable[[ResolvedSinkConfig], SinkPort],
    ) -> None:
        """注册 Sink factory。

        Args:
            type_name: 与 ResolvedSinkConfig.type 一致的类型名。
            factory: 接收 ResolvedSinkConfig 并返回 SinkPort 的工厂。

        Raises:
            ConfigError: 同类型已注册。
        """
        if type_name in self._factories:
            raise ConfigError(f"Sink type '{type_name}' is already registered")
        self._factories[type_name] = factory

    def registered_types(self) -> tuple[str, ...]:
        """返回已注册 Sink 类型名（排序后快照）。"""
        return tuple(sorted(self._factories))

    def create(self, cfg: ResolvedSinkConfig) -> SinkPort:
        """按 ``cfg.type`` 创建 Sink。

        Raises:
            ConfigError: 类型未注册。
        """
        factory = self._factories.get(cfg.type)
        if factory is None:
            raise ConfigError(
                f"Unknown sink type '{cfg.type}'. "
                f"Registered types: {sorted(self._factories)}"
            )
        return factory(cfg)
