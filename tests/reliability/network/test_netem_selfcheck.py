"""Reliability/network：netem 故障注入自检——证明损伤真实作用于业务流量。

NET-01..05 的行为断言只有在「流量确实穿越被注入的 qdisc」时才有意义；
``tc qdisc add`` 成功 ≠ 流量被损伤——同命名空间拓扑下本机投递走 lo
回环、绕过 veth qdisc，netem 规则形同虚设，行为测试会空转出假 PASS。
本文件是整条 netem 链路的前置自证：

- 路由自检：到 server 端 IP 的路由必须出 veth 接口（不得是 lo）；
- 延迟自检：delay 50ms 下真实 Modbus 请求 RTT 显著高于基线；
- 丢包自检：loss 100% 下真实请求失败，清除后恢复。

任一自检失败说明拓扑或 qdisc 挂载错误，NET-01..05 的 PASS 不可信。
需要 root（CAP_NET_ADMIN）；无权限整套 skip（NOT_RUN，不是 PASS）。
"""

from __future__ import annotations

import statistics
import subprocess
import time
from typing import Any

import pytest

from tests.performance.netem import NetemScenario
from tests.reliability.network.conftest import NetemLink

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

#: 延迟自检阈值：50ms egress 延迟应使请求 RTT 至少增加 30ms
#: （响应方向不挂 qdisc，理论增量 ≈ 50ms；阈值留足调度噪声余量）。
_DELAY_MS = 50
_MIN_RTT_DELTA_S = 0.030
#: RTT 采样次数，取 median 抗抖动。
_SAMPLES = 10
#: 丢包自检的客户端超时（黑洞下必须快速失败，不拖长测试）。
_LOSS_TIMEOUT_S = 1.0


def _route_iface(ip: str) -> str:
    """``ip route get <ip>`` 的出接口名（只读操作，不需要特权）。"""
    result = subprocess.run(
        ["ip", "route", "get", ip], capture_output=True, text=True, check=True
    )
    tokens = result.stdout.split()
    return tokens[tokens.index("dev") + 1] if "dev" in tokens else ""


async def _read_rtt_s(client: Any) -> float:
    """一次真实 Modbus 读请求的往返时间（秒）。

    Any 仅隔离 pymodbus 未类型化 client，与同文件延迟导入保持一致。
    """
    start = time.perf_counter()
    response = await client.read_holding_registers(100, count=2, device_id=1)
    elapsed = time.perf_counter() - start
    assert not response.isError(), "自检读请求返回 Modbus 异常响应"
    return elapsed


async def _median_rtt_s(client: Any, samples: int = _SAMPLES) -> float:
    return statistics.median([await _read_rtt_s(client) for _ in range(samples)])


class TestNetemFaultInjectionSelfCheck:
    async def test_route_uses_veth_not_loopback(self, netem_link: NetemLink) -> None:
        """路由自检：到 server 端 IP 的业务流量必须出 veth 接口。

        若出接口是 lo，说明流量根本没穿越 veth pair——挂在 veth 上的
        netem 规则全部无效，后续所有降级断言都是空转。
        """
        iface = _route_iface(netem_link.device_host)
        assert iface == netem_link.pair.name_client, (
            f"到 {netem_link.device_host} 的路由出接口是 {iface!r}，"
            f"应为 {netem_link.pair.name_client!r}——流量未走 veth，netem 不生效"
        )

    async def test_delay_actually_applies_to_traffic(self, netem_link: NetemLink) -> None:
        """延迟自检：delay 50ms 后真实 Modbus 请求 RTT 显著上升。

        baseline 与 fault 各取 median，要求 fault ≥ baseline + 30ms——
        qdisc 挂错接口时两侧 RTT 无差异，立即暴露。
        """
        from pymodbus.client import AsyncModbusTcpClient

        client = AsyncModbusTcpClient(
            netem_link.device_host,
            port=netem_link.device_port,
            timeout=5,
            retries=0,
        )
        await client.connect()
        try:
            baseline = await _median_rtt_s(client)
            netem_link.apply(NetemScenario(name="selfcheck_delay", delay_ms=_DELAY_MS))
            try:
                fault = await _median_rtt_s(client)
            finally:
                netem_link.clear()
        finally:
            client.close()

        assert fault >= baseline + _MIN_RTT_DELTA_S, (
            f"delay {_DELAY_MS}ms 未作用到业务流量：baseline RTT {baseline * 1000:.1f}ms，"
            f"fault RTT {fault * 1000:.1f}ms——qdisc 可能挂错接口"
        )

    async def test_blackhole_actually_drops_traffic(self, netem_link: NetemLink) -> None:
        """丢包自检：loss 100% 下真实请求超时失败，清除后同一连接恢复。"""
        from pymodbus.client import AsyncModbusTcpClient
        from pymodbus.exceptions import ModbusIOException

        client = AsyncModbusTcpClient(
            netem_link.device_host,
            port=netem_link.device_port,
            timeout=_LOSS_TIMEOUT_S,
            retries=0,
        )
        await client.connect()
        try:
            await _read_rtt_s(client)  # 故障前基线：链路可用

            netem_link.apply(NetemScenario(name="selfcheck_blackhole", loss_pct=100))
            with pytest.raises(ModbusIOException):
                await _read_rtt_s(client)

            netem_link.clear()
            await _read_rtt_s(client)  # 清除后同一连接恢复——证明是 qdisc 而非服务死亡
        finally:
            netem_link.clear()
            client.close()
