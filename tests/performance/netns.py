"""跨 net namespace 的压测/soak 链路（决策 2 的修正形态）。

拓扑与 ``tests/reliability/network/conftest.py`` 一致::

    root namespace                      netns whp<pid>
    ┌────────────────┐   veth pair   ┌──────────────────────┐
    │ vpc<pid>       ├───────────────┤ vps<pid>             │
    │ 10.99.0.1      │               │ 10.99.0.2            │
    │ (collector)    │               │ (perf/soak server)   │
    └────────────────┘               └──────────────────────┘

为什么必须跨命名空间：两端同命名空间时，本机进程访问 veth 端点 IP 走
local 路由表经 lo 回环投递，**不经过 veth 接口的 qdisc**——挂在 veth 上
的 netem 规则形同虚设（此前 run_benchmark / soak netem 用例正是这个
空转形态）。server 以子进程跑在对端 namespace 后，报文真实穿越 veth
pair，netem 才作用在链路上。

需要 root（CAP_NET_ADMIN）；不自动 sudo（决策 10）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.performance.veth import VethManager, VethPair

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class NetnsLink:
    """跨命名空间链路句柄：veth 对 + server 所在 namespace + server 子进程。"""

    pair: VethPair
    namespace: str
    server_proc: subprocess.Popen[bytes]


def _ip(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


async def _wait_link_ready(
    proc: subprocess.Popen[bytes], host: str, port: int, timeout: float = 15.0
) -> None:
    """从 root namespace 轮询 TCP 连接直到 server 可达（同时验证 veth 通路）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"netns server 提前退出（rc={proc.returncode}）")
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.0
            )
        except (TimeoutError, OSError):
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"等待 {host}:{port} 可达超时（{timeout}s）") from None
            await asyncio.sleep(0.2)
        else:
            writer.close()
            await writer.wait_closed()
            return


@asynccontextmanager
async def netns_perf_server(
    protocol: str,
    port: int,
    num_points: int = 600,
    device_id: str = "perf-iec104",
    log_path: Path | None = None,
) -> AsyncIterator[NetnsLink]:
    """创建跨 namespace 链路并在对端启动压测 server 子进程。

    Yields:
        NetnsLink：``pair.ip_server`` 是采集端应连接的地址；netem 两端
        分别用 ``NetemController(pair.name_client)`` 与
        ``NetemController(pair.name_server, namespace=link.namespace)``。
    """
    pid = os.getpid()
    namespace = f"whp{pid}"
    manager = VethManager(
        client_name=f"vpc{pid}",
        server_name=f"vps{pid}",
        server_namespace=namespace,
    )
    if not manager.check_permission():
        raise PermissionError("netns 压测链路需要 root（CAP_NET_ADMIN）")

    server_proc: subprocess.Popen[bytes] | None = None
    try:
        # 幂等预清理：上一次异常退出的残留 namespace / 接口。
        _ip(["ip", "link", "del", f"vpc{pid}"])
        _ip(["ip", "netns", "del", namespace])
        result = _ip(["ip", "netns", "add", namespace])
        if result.returncode != 0:
            raise RuntimeError(f"创建 net namespace 失败: {result.stderr.strip()}")

        pair = manager.create()
        log = log_path.open("wb") if log_path is not None else subprocess.DEVNULL
        server_proc = subprocess.Popen(  # noqa: S603 - 测试基础设施，参数全部为内部常量
            [
                "ip",
                "netns",
                "exec",
                namespace,
                sys.executable,
                "-m",
                "tests.fixtures.servers.perf_ns_main",
                "--protocol",
                protocol,
                "--host",
                pair.ip_server,
                "--port",
                str(port),
                "--num-points",
                str(num_points),
                "--device-id",
                device_id,
            ],
            cwd=REPO_ROOT,  # `-m` 以 cwd 入 sys.path，保证 tests.* 可导入
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        await _wait_link_ready(server_proc, pair.ip_server, port)
        yield NetnsLink(pair=pair, namespace=namespace, server_proc=server_proc)
    finally:
        if server_proc is not None and server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
        manager.destroy()
        _ip(["ip", "netns", "del", namespace])
