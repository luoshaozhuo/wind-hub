"""协议 Driver 注册表。

各协议模块导入时通过 register_protocol 自注册 factory；组合根仅按
DeviceConfig.protocol 名称创建 ProtocolPort，不直接依赖具体 Driver 类。
注册表不创建连接，也不持有 Driver 实例。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from wind_hub.domain.model.errors import ConfigError

if TYPE_CHECKING:
    from wind_hub.config.schema import DeviceConfig
    from wind_hub.domain.port.outbound import ProtocolPort


class ProtocolRegistry:
    """协议名到 ProtocolPort factory 的进程内注册表。"""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[DeviceConfig], ProtocolPort]] = {}

    def register(
        self,
        name: str,
        factory: Callable[[DeviceConfig], ProtocolPort],
    ) -> None:
        """注册协议 Driver factory。

        Args:
            name: 与 DeviceConfig.protocol 一致的协议名。
            factory: 接收 DeviceConfig 并返回 ProtocolPort 的工厂。

        Raises:
            ConfigError: 同名协议已注册。
        """
        if name in self._factories:
            raise ConfigError(f"Protocol driver '{name}' is already registered")
        self._factories[name] = factory

    def create(self, name: str, cfg: DeviceConfig) -> ProtocolPort:
        """按协议名创建 Driver。

        Args:
            name: 协议名。
            cfg: 目标设备配置。

        Returns:
            新建 ProtocolPort 实例。

        Raises:
            ConfigError: 协议未注册。
        """
        factory = self._factories.get(name)
        if factory is None:
            raise ConfigError(
                f"Unknown protocol driver '{name}'. "
                f"Registered drivers: {sorted(self._factories)}"
            )
        return factory(cfg)

    def names(self) -> list[str]:
        """返回已注册协议名，按字典序排列。"""
        return sorted(self._factories)

    def is_registered(self, name: str) -> bool:
        """判断协议名是否已注册 factory。"""
        return name in self._factories


# ---------------------------------------------------------------------------
# 进程级注册表实例
# ---------------------------------------------------------------------------

protocol_registry = ProtocolRegistry()


def register_protocol(
    name: str,
) -> Callable[
    [Callable[[DeviceConfig], ProtocolPort]],
    Callable[[DeviceConfig], ProtocolPort],
]:
    """创建协议自注册 decorator。

    Args:
        name: 协议名。

    Returns:
        保持原 factory 不变、同时将其注册到全局 registry 的 decorator。
    """

    def _decorator(
        factory: Callable[[DeviceConfig], ProtocolPort],
    ) -> Callable[[DeviceConfig], ProtocolPort]:
        protocol_registry.register(name, factory)
        return factory

    return _decorator
