"""Network 降级测试共享 fixture：跨命名空间 veth 链路 + netem 控制器。

拓扑::

    root namespace                      netns whn<pid>
    ┌────────────────┐   veth pair   ┌──────────────────────┐
    │ Collector      │ vnc<pid>      │ vns<pid>             │
    │ (被测进程)      ├───────────────┤ ModbusMockServer     │
    │                │ 10.99.X.1     │ (子进程) 10.99.X.2   │
    └────────────────┘               └──────────────────────┘
                   ↑ netem 挂在 root 侧接口 egress（请求方向）

为什么必须跨命名空间：两端同命名空间时，本机进程访问 veth 端点 IP 走
local 路由表经 lo 回环投递，不经过 veth 接口的 qdisc——挂在 veth 上的
netem 规则对这类流量不生效（故障根本没注入，测试会是空转）。跨命名
空间后报文真实穿越 veth pair，netem 才作用在链路上。

请求/响应协议只需损伤请求方向即可完整决定驱动的超时/重传/重连行为；
响应方向（root 侧 ingress）不挂 qdisc。

需要 root（CAP_NET_ADMIN）；无权限时 skip（决策 10：不自动 sudo，
与 tests/performance 同一约定）——报告中计为 NOT_RUN，不是 PASS。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.performance.netem import NetemController, NetemScenario
from tests.performance.veth import VethManager, VethPair

# 复用 system conftest 的 collector harness（import 即注册，不复制实现）。
from tests.system.conftest import collector_factory  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[3]

#: namespace 内的独立端口空间，固定端口即可（不与 root 命名空间冲突）。
MODBUS_NS_PORT = 15520


def _ip(args: list[str]) -> subprocess.CompletedProcess[str]:
    """执行 ip 命令（不 check，清理路径允许失败）。"""
    return subprocess.run(args, capture_output=True, text=True, check=False)


@dataclass
class NetemLink:
    """一条可注入损伤的 Modbus 链路（设备在 veth 对端 namespace）。"""

    pair: VethPair
    namespace: str
    server_proc: subprocess.Popen[bytes]
    netem: NetemController

    @property
    def device_host(self) -> str:
        return self.pair.ip_server

    @property
    def device_port(self) -> int:
        return MODBUS_NS_PORT

    def apply(self, scenario: NetemScenario) -> None:
        """在 root 侧接口 egress（客户端请求方向）应用 netem 场景。"""
        self.netem.apply(scenario)

    def clear(self) -> None:
        self.netem.clear()

    async def outage(self, duration_s: float) -> None:
        """100% 丢包 ``duration_s`` 秒后恢复（阻塞协程；用 create_task 并发）。"""
        await self.netem.simulate_outage(duration_s)


@pytest.fixture
async def netem_link(tmp_path: Path) -> AsyncIterator[NetemLink]:
    pid = os.getpid()
    namespace = f"whn{pid}"
    manager = VethManager(
        client_name=f"vnc{pid}",
        server_name=f"vns{pid}",
        client_ip=f"10.99.{pid % 200 + 1}.1",
        server_ip=f"10.99.{pid % 200 + 1}.2",
        server_namespace=namespace,
    )
    if not manager.check_permission():
        pytest.skip("netem/veth 需要 root 权限（决策 10：不自动 sudo）——NOT_RUN")

    netem = NetemController(f"vnc{pid}")
    server_proc: subprocess.Popen[bytes] | None = None
    try:
        # 幂等预清理：上一次异常退出的残留 namespace / 接口。
        _ip(["ip", "link", "del", f"vnc{pid}"])
        _ip(["ip", "netns", "del", namespace])
        result = _ip(["ip", "netns", "add", namespace])
        if result.returncode != 0:
            raise RuntimeError(f"创建 net namespace 失败: {result.stderr.strip()}")

        pair = manager.create()
        server_log = (tmp_path / "modbus_ns_server.log").open("wb")
        server_proc = subprocess.Popen(  # noqa: S603 - 测试基础设施，参数全部为内部常量
            [
                "ip",
                "netns",
                "exec",
                namespace,
                sys.executable,
                "-m",
                "tests.fixtures.servers.modbus_ns_main",
                "--host",
                pair.ip_server,
                "--port",
                str(MODBUS_NS_PORT),
            ],
            cwd=REPO_ROOT,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        await _wait_link_ready(server_proc, pair.ip_server, MODBUS_NS_PORT)
        yield NetemLink(
            pair=pair, namespace=namespace, server_proc=server_proc, netem=netem
        )
    finally:
        netem.clear()
        if server_proc is not None and server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
        manager.destroy()
        _ip(["ip", "netns", "del", namespace])


async def _wait_link_ready(
    proc: subprocess.Popen[bytes], host: str, port: int, timeout: float = 15.0
) -> None:
    """从 root namespace 轮询 TCP 连接直到从站可达（同时验证 veth 通路）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"modbus namespace server 提前退出（rc={proc.returncode}）")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.0
            )
        except (TimeoutError, OSError):
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    f"等待 {host}:{port} 可达超时（{timeout}s）"
                ) from None
            await asyncio.sleep(0.2)
        else:
            writer.close()
            await writer.wait_closed()
            return
