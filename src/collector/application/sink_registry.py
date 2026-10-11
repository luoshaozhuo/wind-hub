"""Collector Sink 工厂注册表，SinkPort 契约由 Core 统一定义。"""


from __future__ import annotations

from collections.abc import Callable

from core.application import ConfigError
from core.application.port import SinkPort
from core.domain import Sink

SinkFactory = Callable[[Sink], SinkPort]


class SinkRegistry:
    """Sink 类型名到 SinkPort factory 的注册表。"""

    def __init__(self) -> None:
        self._factories: dict[str, SinkFactory] = {}

    def register(self, type_name: str, factory: SinkFactory) -> None:
        """注册 Sink factory。

        Args:
            type_name: 与 Sink.kind 一致的类型名。
            factory: 接收 Sink 并返回 SinkPort 的工厂。

        Raises:
            ConfigError: 同类型已注册。
        """
        if type_name in self._factories:
            raise ConfigError(f"Sink type '{type_name}' is already registered")
        self._factories[type_name] = factory

    def override(self, type_name: str, factory: SinkFactory) -> None:
        """替换已注册类型的 factory（测试/资格工具注入计量或空 Sink 的 seam）。

        与 :meth:`register` 不同，本方法要求类型已存在——只允许替换内置
        类型的构造行为，不允许借覆盖路径新增配置白名单外的类型。

        Raises:
            ConfigError: 类型未注册。
        """
        if type_name not in self._factories:
            raise ConfigError(
                f"Sink type '{type_name}' is not registered. "
                f"Registered types: {sorted(self._factories)}"
            )
        self._factories[type_name] = factory

    def registered_types(self) -> tuple[str, ...]:
        """返回已注册 Sink 类型名（排序后快照）。"""
        return tuple(sorted(self._factories))

    def create(self, cfg: Sink) -> SinkPort:
        """按 ``cfg.kind`` 创建 Sink。

        Raises:
            ConfigError: 类型未注册。
        """
        factory = self._factories.get(cfg.kind)
        if factory is None:
            raise ConfigError(
                f"Unknown sink type '{cfg.kind}'. " f"Registered types: {sorted(self._factories)}"
            )
        return factory(cfg)

__all__ = ["SinkFactory", "SinkRegistry"]
