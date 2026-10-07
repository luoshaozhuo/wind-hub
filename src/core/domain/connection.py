"""Shared Domain 设备通信接入模型。"""

from __future__ import annotations

from dataclasses import dataclass

from .identities import ConnectionId, DeviceId


@dataclass(frozen=True, slots=True)
class ConnectionEndpoint:
    """设备可达的逻辑通信端点。

    host/port 是跨协议稳定概念；协议专有连接参数不进入 Domain。
    """

    host: str
    port: int | None = None

    def __post_init__(self) -> None:
        host = self.host.strip()
        if not host:
            raise ValueError("endpoint host must not be empty")
        if self.port is not None:
            if isinstance(self.port, bool) or not isinstance(self.port, int):
                raise ValueError("endpoint port must be an integer or null")
            if not 1 <= self.port <= 65535:
                raise ValueError("endpoint port must be between 1 and 65535")
        object.__setattr__(self, "host", host)


@dataclass(frozen=True, slots=True)
class DeviceConnection:
    """一台 Device 的一次独立通信接入定义。"""

    connection_id: ConnectionId
    device_id: DeviceId
    endpoint: ConnectionEndpoint
    enabled: bool = True

    def __post_init__(self) -> None:
        connection_id = self.connection_id.strip()
        device_id = self.device_id.strip()
        if not connection_id:
            raise ValueError("connection_id must not be empty")
        if not device_id:
            raise ValueError("device_id must not be empty")
        if not isinstance(self.enabled, bool):
            raise ValueError("connection enabled must be boolean")

        object.__setattr__(
            self,
            "connection_id",
            ConnectionId(connection_id),
        )
        object.__setattr__(self, "device_id", DeviceId(device_id))
