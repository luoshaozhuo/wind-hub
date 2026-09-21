"""probe ports 的数据模型——纯逻辑、无 I/O，可独立单测。

:class:`PortResult` 是单端口的四态结果（决策 4 + step20 任务 0.1）；
:class:`PortScanResult` 聚合一个 IP 的全部端口结果。服务识别是纯端口
映射（决策 5），不做 banner grabbing。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wind_hub.config.ports_config import PortsConfig


class PortState(str, Enum):
    """端口四态（决策 4 + step20 任务 0.1）。

    ``CLOSED``（收到 RST / ConnectionRefusedError）、``TIMEOUT``（防火
    墙 DROP 等无响应）与 ``UNREACHABLE``（EHOSTUNREACH / ENETUNREACH，
    路由不存在）的运维含义不同——主机能达但端口未监听、中间可能有包
    过滤、本机路由缺失是三种完全不同的现场处置——因此必须区分，不
    能合并为「不通」。
    """

    OPEN = "open"
    CLOSED = "closed"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"


def guess_service(port: int, config: PortsConfig | None = None) -> str | None:
    """端口 → 服务名（纯映射）；未识别的端口返回 ``None``。

    查端口配置的 ``mapping``（configs/ports.yaml 驱动）；``config`` 为
    ``None`` 时用内置工业协议映射（配置文件缺失的兜底）。
    """
    if config is None:
        from wind_hub.config.ports_config import default_ports_config

        config = default_ports_config()
    return config.mapping.get(port)


@dataclass(frozen=True)
class PortResult:
    """单个端口的扫描结果。"""

    port: int
    state: PortState
    service_guess: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON/YAML 输出用的字典形式。"""
        return {
            "port": self.port,
            "state": self.state.value,
            "service_guess": self.service_guess,
        }


@dataclass(frozen=True)
class PortScanResult:
    """一个 IP 的端口扫描结果。

    Attributes:
        ip: 目标 IP。
        ports: 全部端口结果（按端口号升序）。
        total: 扫描的端口总数。
        open_count: 其中 open 的数量。
    """

    ip: str
    ports: list[PortResult]
    total: int
    open_count: int

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON/YAML 输出用的字典形式。"""
        return {
            "ip": self.ip,
            "total": self.total,
            "open_count": self.open_count,
            "ports": [p.to_dict() for p in self.ports],
        }
