"""共享设备通信接入配置。

DeviceConnection 只描述一个稳定的现场接入定义，不承载连接状态、重连策略、
Worker 生命周期或采集/控制语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from core.domain import ConnectionId, DeviceId

from ..errors import ConfigError

ConnectionOptionValue: TypeAlias = str | int | float | bool | None


def _freeze_options(
    value: Mapping[str, ConnectionOptionValue],
) -> Mapping[str, ConnectionOptionValue]:
    """返回连接扩展参数的只读浅拷贝，并拒绝非有限浮点值。"""
    options = dict(value)
    for key, item in options.items():
        if isinstance(item, float) and not isfinite(item):
            raise ConfigError(
                f"connection option '{key}' must be finite"
            )
    return MappingProxyType(options)


@dataclass(frozen=True, slots=True)
class ConnectionEndpoint:
    """现场通信端点。

    host/port 是跨协议常见属性；options 保存协议专有的连接级参数，例如
    ADS target_net_id、Modbus unit_id 或连接超时等。进程级本机身份不放在这里。
    """

    host: str
    port: int | None = None
    options: Mapping[str, ConnectionOptionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        host = self.host.strip()
        if not host:
            raise ConfigError("endpoint host must not be empty")
        if self.port is not None:
            if isinstance(self.port, bool) or not isinstance(self.port, int):
                raise ConfigError("endpoint port must be an integer or null")
            if not 1 <= self.port <= 65535:
                raise ConfigError("endpoint port must be between 1 and 65535")

        object.__setattr__(self, "host", host)
        object.__setattr__(self, "options", _freeze_options(self.options))


@dataclass(frozen=True, slots=True)
class DeviceConnection:
    """一台 Device 的一次独立通信接入定义。

    PointTable 不在此重复保存，而由 Device -> DeviceModel -> PointTable 唯一解析。
    同一 Device 可以拥有多个 DeviceConnection，endpoint 允许相同或不同。
    """

    connection_id: ConnectionId
    device_id: DeviceId
    endpoint: ConnectionEndpoint
    enabled: bool = True

    def __post_init__(self) -> None:
        connection_id = self.connection_id.strip()
        device_id = self.device_id.strip()
        if not connection_id:
            raise ConfigError("connection_id must not be empty")
        if not device_id:
            raise ConfigError("device_id must not be empty")
        if not isinstance(self.enabled, bool):
            raise ConfigError("connection enabled must be boolean")

        object.__setattr__(self, "connection_id", ConnectionId(connection_id))
        object.__setattr__(self, "device_id", DeviceId(device_id))
