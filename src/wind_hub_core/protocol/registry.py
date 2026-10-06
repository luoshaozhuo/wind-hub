"""协议 Driver 注册表。

``ProtocolRegistry`` 只保存「协议名 → ProtocolPort factory」，不建立连接、
不持有 Driver 实例，也不包含任何进程 Runtime 状态。内置 Driver 的注册发生在
组合根显式调用的 :func:`build_protocol_registry`，不依赖 import side effect
或 decorator 副作用。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from wind_hub_core.model.errors import ConfigError

if TYPE_CHECKING:
    from wind_hub_core.config import DeviceConfig
    from wind_hub_core.protocol.port import ProtocolPort


class ProtocolRegistry:
    """协议名到 ProtocolPort factory 的注册表。"""

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

    def registered_names(self) -> tuple[str, ...]:
        """返回已注册协议名（排序后快照）。"""
        return tuple(sorted(self._factories))

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

    def create_for(self, cfg: DeviceConfig) -> ProtocolPort:
        """按 ``cfg.protocol`` 创建 Driver——DeviceConfig 驱动的标准入口。"""
        return self.create(cfg.protocol, cfg)


def build_protocol_registry() -> ProtocolRegistry:
    """构造注册好全部内置 Driver 的注册表。

    这是内置协议注册的唯一入口；Collector / Commander 组合根各持有一份实例，
    进程间、进程内均不存在共享全局注册表。可选第三方依赖仍在 Driver 实际
    使用时延迟导入，注册本身不建立网络连接。
    """
    from wind_hub_core.protocol.ads.driver import ADSDriver
    from wind_hub_core.protocol.iec104.driver import IEC104Driver
    from wind_hub_core.protocol.modbus.driver import ModbusDriver

    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("modbus", ModbusDriver)
    registry.register("iec104", IEC104Driver)
    return registry
