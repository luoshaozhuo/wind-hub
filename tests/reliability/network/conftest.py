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

执行策略（默认真实注入，不允许静默 skip）：

1. euid==0（真实 root / CI root runner）：直接执行；
2. 非 root 但 user namespace 可用：**默认**在 ``unshare -rmn`` 隔离命名
   空间子进程里重跑本目录全部用例（命名空间内 euid=0 + CAP_NET_ADMIN，
   与宿主机网络完全隔离，不需要 sudo，不违反「不自动 sudo」约定）——
   由本文件底部的 collection/sessionfinish hook 透明完成；
3. 两者都不可用：fixture 诚实 skip，报告中计 NOT_RUN，不是 PASS。
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
        pytest.skip(
            "netem/veth 需要 root 或 user namespace（unshare -rmn），"
            "当前环境均不可用——NOT_RUN"
        )

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


# ---------------------------------------------------------------------------
# 默认真实 root 执行：非 root 时转入 user namespace 子进程重跑本目录
# ---------------------------------------------------------------------------

NETWORK_DIR = Path(__file__).resolve().parent

#: 子进程环境标记——防重入（命名空间内 euid==0 本身已是充分条件）。
_NS_CHILD_ENV = "WIND_HUB_NETEM_NS_CHILD"


def _userns_netem_available() -> bool:
    """探测非 root 下的提权路径：user+mount+net namespace 内获得
    euid=0 + CAP_NET_ADMIN（隔离环境，不影响宿主机网络，不需要 sudo）。"""
    if os.environ.get(_NS_CHILD_ENV):
        return False
    result = subprocess.run(["unshare", "-rmn", "true"], capture_output=True, check=False)
    return result.returncode == 0


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """非 root 且可进 user namespace 时，把本目录用例移出当前进程，交给
    sessionfinish 阶段的命名空间子进程真实执行（子进程输出直接继承到
    终端，退出码回传——失败即整体 FAIL，不存在静默 PASS）。"""
    if os.geteuid() == 0 or getattr(config.option, "collectonly", False):
        return
    netem_items = [i for i in items if i.path.is_relative_to(NETWORK_DIR)]
    if not netem_items or not _userns_netem_available():
        return
    items[:] = [i for i in items if i not in netem_items]
    config.hook.pytest_deselected(items=netem_items)
    config._netem_ns_deferred = True  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    deferred = getattr(session.config, "_netem_ns_deferred", False)
    if not deferred:
        return
    print(
        "\n===== netem 矩阵：当前非 root，转入 unshare -rmn 隔离命名空间"
        "子进程真实执行（tc/veth 注入全部生效）====="
    )
    result = subprocess.run(
        [
            "unshare",
            "-rmn",
            "bash",
            "-c",
            "mount -t tmpfs tmpfs /run && mkdir -p /run/netns && ip link set lo up && "
            f"exec {sys.executable} -m pytest {NETWORK_DIR} -q",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, _NS_CHILD_ENV: "1"},
        check=False,
    )
    if result.returncode != 0:
        session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)
    elif exitstatus == int(pytest.ExitCode.NO_TESTS_COLLECTED):
        # 全部用例被 defer（如单独跑本目录）时，父进程没有可执行用例，
        # 结果以子进程为准。
        session.exitstatus = int(pytest.ExitCode.OK)
