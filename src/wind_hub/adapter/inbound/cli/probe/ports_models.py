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


# 已知协议端口映射（决策 5）：工业协议为主，辅以少量通用管理端口。
# step24 起为内置兜底值——部署应通过 configs/ports.yaml 提供完整映射
# （见 wind_hub.config.ports_config），未加载配置时沿用本表。
SERVICE_MAP: dict[int, str] = {
    502: "modbus",
    2404: "iec104",
    48898: "ads",
    4840: "opc-ua",
    44818: "ethernet-ip",
    80: "http",
    443: "https",
    22: "ssh",
    23: "telnet",
}


def guess_service(port: int, config: PortsConfig | None = None) -> str | None:
    """端口 → 服务名（纯映射）；未识别的端口返回 ``None``。

    ``config`` 提供时查 ``config.service_map``（配置文件驱动，step24）；
    否则回落到内置 :data:`SERVICE_MAP`（向后兼容）。
    """
    if config is not None:
        return config.service_map.get(port)
    return SERVICE_MAP.get(port)


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
