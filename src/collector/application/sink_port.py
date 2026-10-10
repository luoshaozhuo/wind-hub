"""Sink outbound port 与类型注册表（Application 层）。

SinkPort 由 application 层（Runtime 的投递编排）消费；文件 / Kafka /
数据库 / 协议服务端等实现位于 ``collector.infrastructure.sink``，由
组合根装配注入。

SinkRegistry 与 ``core.infrastructure.ProtocolRegistry`` 同一设计语言：
注册表只保存「类型 → factory」，不持有 Sink 运行实例；内置 Sink 的
注册发生在组合根显式调用的装配函数，不依赖 import side effect。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from core.application import ConfigError, ConnectionHealth
from core.application.sink_config import ResolvedSinkConfig

from ..domain.point_value import PointValue


class SinkPort(Protocol):
    """点值批量交付的 outbound port。

    实现可以是文件、Kafka、数据库等外部介质。Sink 只接收 Runtime 已
    路由好的 PointValue，不感知 Task Definition、设备协议或控制面。
    """

    async def open(self) -> None:
        """初始化外部 Sink 资源。"""
        ...

    async def close(self) -> None:
        """释放 Sink 资源。

        必须支持幂等调用；实现应在关闭前处理自己的缓存和连接。
        """
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """写入一批 PointValue。

        Args:
            batch: Runtime 已决定路由到该 Sink 的点值。
        """
        ...

    async def flush(self) -> None:
        """强制把实现内部缓冲提交到底层介质。

        优雅停机阶段会调用该方法，以尽量降低已进入 Sink 的数据丢失风险。
        """
        ...

    def health(self) -> ConnectionHealth:
        """返回缓存的 Sink 健康状态；不得在该同步接口中执行阻塞 I/O。"""
        ...


@runtime_checkable
class ExclusiveOpenSinkPort(Protocol):
    """可选 Sink 能力：重建时必须先关闭旧实例才能打开新实例。

    典型场景是监听固定 TCP 端口的 server 型 Sink。Runtime 仅在实现该
    能力且 exclusive_open 为 True 时采用 close-first 替换策略。
    """

    @property
    def exclusive_open(self) -> bool:
        """是否要求同名 Sink 重建采用 close-first。"""
        ...


SinkFactory = Callable[[ResolvedSinkConfig], SinkPort]


class SinkRegistry:
    """Sink 类型名到 SinkPort factory 的注册表。"""

    def __init__(self) -> None:
        self._factories: dict[str, SinkFactory] = {}

    def register(self, type_name: str, factory: SinkFactory) -> None:
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

    def create(self, cfg: ResolvedSinkConfig) -> SinkPort:
        """按 ``cfg.type`` 创建 Sink。

        Raises:
            ConfigError: 类型未注册。
        """
        factory = self._factories.get(cfg.type)
        if factory is None:
            raise ConfigError(
                f"Unknown sink type '{cfg.type}'. " f"Registered types: {sorted(self._factories)}"
            )
        return factory(cfg)
