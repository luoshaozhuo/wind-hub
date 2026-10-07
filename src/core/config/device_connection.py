"""设备静态通信接入配置。

DeviceConnection 表达一台 Device 与一张具体 PointTable 的独立接入定义。
PointTable.protocol 唯一决定协议语义，因此 DeviceConnection 不重复保存 protocol。

运行时约束不进入本配置模型：一个独立 Worker 运行一个 DeviceConnection，
一个 DeviceConnection 在该 Worker 中只建立一个活动协议连接。Worker、Session、
重连状态等属于 Application / Runtime，而不是 Shared Domain。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, TypeAlias

from core.domain import ConnectionId, DeviceId, PointTableId

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
    参数，例如 ADS target_net_id、local_ams_net_id、Modbus unit_id、超时时间等。
    不同 DeviceConnection 的 endpoint 可以相同，也可以不同。
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
    """一台现场设备针对其型号点表的一次独立通信接入定义。

    - device_id 指定具体设备；
    - point_table_id 必须与该设备 DeviceModel.point_table_id 一致；
    - PointTable.protocol 唯一决定协议；
    - endpoint 描述该连接实例的现场连接参数；
    - connection_id 是独立接入单元的稳定身份，可供 Worker placement、
      start/stop、health、reload 与诊断等运行能力引用。

    同一个设备可以存在多个 DeviceConnection；它们可以使用相同或不同 endpoint。
    """

    connection_id: ConnectionId
    device_id: DeviceId
    point_table_id: PointTableId
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

        object.__setattr__(self, "connection_id", ConnectionId(connection_id))
        object.__setattr__(self, "device_id", DeviceId(device_id))
        object.__setattr__(self, "point_table_id", PointTableId(point_table_id))
