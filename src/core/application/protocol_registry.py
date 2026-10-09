"""共享协议 Driver 显式注册表。"""

from __future__ import annotations

from collections.abc import Callable

from core.application.errors import ConfigError
from core.application.port import ProtocolPort
from core.domain import ConnectionEndpoint, PointTable, Protocol, ProtocolOptions

ProtocolFactory = Callable[
    [ConnectionEndpoint, PointTable, ProtocolOptions],
    ProtocolPort,
]


class ProtocolRegistry:
    """协议名到 ProtocolPort factory 的显式注册表。

    Registry 只保存工厂，不建立连接、不缓存 Driver 实例，也不依赖 import
    side effect。Collector 与 Commander 可以各自持有独立 Registry 实例。
    """

    def __init__(self) -> None:
        self._factories: dict[str, ProtocolFactory] = {}

    def register(
        self,
        protocol: Protocol | str,
        factory: ProtocolFactory,
    ) -> None:
        """注册一个协议 Driver factory。"""
        name = _protocol_name(protocol)
        if name in self._factories:
            raise ConfigError(f"protocol driver '{name}' is already registered")
        self._factories[name] = factory

    def registered_names(self) -> tuple[str, ...]:
        """返回稳定排序的已注册协议名。"""
        return tuple(sorted(self._factories))

    def create(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
        *,
        reconnect_attempts: int | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
    ) -> ProtocolPort:
        """为指定 Endpoint 与 resolved PointTable 创建协议实例。"""
        protocol = point_table.protocol
        factory = self._factories.get(protocol.name)
        if factory is None:
            raise ConfigError(
                f"unknown protocol driver '{protocol.name}'; "
                f"registered={list(self.registered_names())}"
            )
        driver = factory(endpoint, point_table, protocol_options)
        if reconnect_attempts is None:
            return driver
        from core.application.port.recovery import RecoveryPort, RecoverySettings

        return RecoveryPort(
            driver,
            RecoverySettings(
                reconnect_attempts=reconnect_attempts,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                write_timeout=write_timeout,
            ),
        )


def _protocol_name(protocol: Protocol | str) -> str:
    if isinstance(protocol, Protocol):
        return protocol.name
    name = protocol.strip().lower()
    if not name:
        raise ConfigError("protocol name must not be empty")
    return name
