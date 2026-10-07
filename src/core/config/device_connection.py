"""设备静态通信接入配置。

DeviceConnection 表达一台 Device 与一张具体 PointTable 的唯一接入关系。
PointTable.protocol 唯一决定协议语义，因此 DeviceConnection 不重复保存 protocol。

运行时约束不进入本配置模型：一个独立 Worker 运行一个 DeviceConnection，
一个 DeviceConnection 在该 Worker 中只建立一个活动协议连接。Worker、Session、
重连状态等属于 Application / Runtime，而不是 Shared Domain。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, TypeAlias

ConnectionOptionValue: TypeAlias = str | int | float | bool | None


def _freeze_options(
    value: Mapping[str, ConnectionOptionValue],
) -> Mapping[str, ConnectionOptionValue]:
    """返回连接扩展参数的只读浅拷贝。"""
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class ConnectionEndpoint:
    """现场通信端点。

    host/port 是各协议最常见的公共连接属性；options 保存协议专有的实例级
    参数，例如 ADS target_net_id、Modbus unit_id、超时时间等。
    """

    host: str
    port: int | None = None
    options: Mapping[str, ConnectionOptionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        host = self.host.strip()
        if not host:
            raise ValueError("endpoint host must not be empty")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("endpoint port must be between 1 and 65535")

        object.__setattr__(self, "host", host)
        object.__setattr__(self, "options", _freeze_options(self.options))


@dataclass(frozen=True, slots=True)
class DeviceConnection:
    """一台现场设备与一张具体点表的通信接入定义。

    - device_id 指定具体设备；
    - point_table_id 指定该设备使用的具体点表；
    - PointTable.protocol 唯一决定协议；
    - endpoint 描述该接入实例的现场连接参数；
    - connection_id 是该独立接入单元的稳定身份，可供 Worker placement、
      start/stop、health、reload 与诊断等运行能力引用。

    同一个 ``(device_id, point_table_id)`` 在配置集合中只允许一个
    DeviceConnection；该集合级不变量由 validation 层校验。
    """

    connection_id: str
    device_id: str
    point_table_id: str
    endpoint: ConnectionEndpoint

    def __post_init__(self) -> None:
        connection_id = self.connection_id.strip()
        device_id = self.device_id.strip()
        point_table_id = self.point_table_id.strip()

        if not connection_id:
            raise ValueError("connection_id must not be empty")
        if not device_id:
            raise ValueError("device_id must not be empty")
        if not point_table_id:
            raise ValueError("point_table_id must not be empty")

        object.__setattr__(self, "connection_id", connection_id)
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "point_table_id", point_table_id)
