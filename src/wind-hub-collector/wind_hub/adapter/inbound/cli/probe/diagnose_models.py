"""probe diagnose 的数据模型——纯数据、无 I/O，可独立单测。

诊断按「网络层 → 传输层 → 协议层」组织（决策 2），每层产生一个或多
个 :class:`DiagnoseStep`；整体结论由步骤状态汇聚（决策 6）：任一
``FAIL`` 则整体 ``FAIL``，否则有 ``WARN`` 则 ``WARN``，否则 ``OK``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    """诊断步骤三态（决策 6）。

    - ``OK``：正常；
    - ``WARN``：可疑但不阻塞（如 ARP 无记录但 ICMP 通）；
    - ``FAIL``：明确故障。
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DiagnoseStep:
    """一个诊断步骤（如 "ARP" / "ICMP" / "TCP 2404" / "IEC104"）。

    Attributes:
        name: 步骤名。
        status: 三态结论。
        detail: 详细信息（一行，给人看）。
        suggestion: 可选的处置建议。
        skipped: 上层失败导致本步骤被短路跳过（输出格式中的「⏭️ skip」，
            决策 2 的短路语义）。跳过的步骤不参与整体结论汇聚；
            ``status`` 此时固定为 ``OK`` 占位，消费方应先看 ``skipped``。
    """

    name: str
    status: StepStatus
    detail: str
    suggestion: str | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 输出用的字典形式。"""
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class LocalInfo:
    """本机网络信息（决策 1）。

    Attributes:
        hostname: 主机名。
        ips: ``[(ip/prefix, interface), ...]``——保留前缀长度，子网判断
            与报告展示都需要它；回环接口已过滤。
        default_gateway: 默认网关 IP；无默认路由时为 ``None``。
    """

    hostname: str
    ips: list[tuple[str, str]]
    default_gateway: str | None

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 输出用的字典形式。"""
        return {
            "hostname": self.hostname,
            "ips": [{"ip": ip, "interface": iface} for ip, iface in self.ips],
            "default_gateway": self.default_gateway,
        }


@dataclass(frozen=True)
class DiagnoseResult:
    """一次设备诊断的完整结果（决策 5/6）。"""

    local_info: LocalInfo
    device_id: str
    device_ip: str
    device_port: int
    protocol: str
    steps: list[DiagnoseStep]
    overall: StepStatus
    conclusion: str
    same_subnet: bool
    subnet_detail: str | None = field(default=None)
    """同网段判断的附加说明（如匹配到的本机网段 / "本机回环"）。"""

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 输出用的字典形式。"""
        return {
            "local_info": self.local_info.to_dict(),
            "device_id": self.device_id,
            "device_ip": self.device_ip,
            "device_port": self.device_port,
            "protocol": self.protocol,
            "same_subnet": self.same_subnet,
            "subnet_detail": self.subnet_detail,
            "steps": [s.to_dict() for s in self.steps],
            "overall": self.overall.value,
            "conclusion": self.conclusion,
        }
