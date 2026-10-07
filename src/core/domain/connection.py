"""Shared Domain 设备通信接入模型。"""

from __future__ import annotations

from dataclasses import dataclass


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

