"""veth pair 管理（决策 2）——压测的隔离网络环境。

创建一对虚拟网卡：``veth-wc``（10.99.0.1，wind-hub 客户端侧）↔
``veth-ws``（10.99.0.2，被测设备 server 侧）。tc netem 作用在 veth 上，
不碰物理网卡，压测与宿主机网络互不影响。

``server_namespace`` 可把 server 端挪进指定 net namespace（调用方负责
提前 ``ip netns add``）：两端同命名空间时，本机进程访问 veth 端点 IP
走 local 路由表经 lo 回环投递，**不经过 veth 接口的 qdisc**——netem
规则形同虚设。跨命名空间后流量真实穿越 veth pair，netem 才生效
（reliability 网络降级测试依赖此拓扑）。

需要 root（CAP_NET_ADMIN）；权限检查只做 euid 判定，**不自动 sudo**
（决策 10）。所有命令经 :func:`_run` 执行（模块级 seam，单测可替换）。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class VethPair:
    """虚拟网卡对（创建完成后的句柄）。"""

    name_client: str
    name_server: str
    ip_client: str
    ip_server: str


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """执行一条系统命令（不 check，由调用方按返回码判定）。"""
    return subprocess.run(args, capture_output=True, text=True, check=False)


class VethManager:
    """veth pair 管理器：创建 → 分配 IP → 启用 → 清理，全程幂等。"""

    def __init__(
        self,
        client_name: str = "veth-wc",
        server_name: str = "veth-ws",
        client_ip: str = "10.99.0.1",
        server_ip: str = "10.99.0.2",
        prefix_len: int = 24,
        server_namespace: str | None = None,
    ) -> None:
        self._client_name = client_name
        self._server_name = server_name
        self._client_ip = client_ip
        self._server_ip = server_ip
        self._prefix_len = prefix_len
        #: server 端要挪入的 net namespace；None = 两端同命名空间（压测默认）。
        self._server_namespace = server_namespace

    def check_permission(self) -> bool:
        """root（CAP_NET_ADMIN）判定：只有 euid 0 才能创建 veth。"""
        return os.geteuid() == 0

    def exists(self) -> bool:
        """检查客户端侧接口是否已存在（veth 成对出现，查一端即可）。"""
        return _run(["ip", "link", "show", self._client_name]).returncode == 0

    def create(self) -> VethPair:
        """创建 veth pair 并分配 IP、启用接口（幂等：已存在直接返回）。

        Raises:
            PermissionError: 非 root。
            RuntimeError: ``ip`` 命令执行失败（含命令不存在）。
        """
        if not self.check_permission():
            raise PermissionError("创建 veth 需要 root 权限（决策 10：不自动 sudo）")
        if self.exists():
            return self._pair()

        result = _run(
            [
                "ip",
                "link",
                "add",
                self._client_name,
                "type",
                "veth",
                "peer",
                "name",
                self._server_name,
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(f"创建 veth pair 失败: {result.stderr.strip()}")
        try:
            if self._server_namespace is not None:
                self._move_server_end()
            self._assign_addresses()
        except Exception:
            self.destroy()  # 半截状态必须清理，保证下次 create 可重入
            raise
        return self._pair()

    def destroy(self) -> None:
        """销毁 veth pair（幂等：删除一端即删除整对，不存在则跳过）。"""
        if not self.exists():
            return
        _run(["ip", "link", "del", self._client_name])

    @asynccontextmanager
    async def active(self) -> AsyncIterator[VethPair]:
        """上下文管理器：创建 → yield → 销毁（异常路径也保证清理）。"""
        pair = self.create()
        try:
            yield pair
        finally:
            self.destroy()

    def _pair(self) -> VethPair:
        return VethPair(
            name_client=self._client_name,
            name_server=self._server_name,
            ip_client=self._client_ip,
            ip_server=self._server_ip,
        )

    def _move_server_end(self) -> None:
        """把 server 端挪进目标 net namespace（namespace 必须已存在）。"""
        cmd = ["ip", "link", "set", self._server_name, "netns", self._server_namespace or ""]
        result = _run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"移动 veth 端点失败 ({' '.join(cmd)}): {result.stderr.strip()}")

    def _assign_addresses(self) -> None:
        """分配 IP 并启用两端接口；任一失败抛 RuntimeError。"""
        client_cidr = f"{self._client_ip}/{self._prefix_len}"
        server_cidr = f"{self._server_ip}/{self._prefix_len}"
        # server 端在独立 namespace 时，其配置命令经 ip netns exec 下达。
        ns_exec = (
            ["ip", "netns", "exec", self._server_namespace]
            if self._server_namespace is not None
            else []
        )
        commands = [
            ["ip", "addr", "add", client_cidr, "dev", self._client_name],
            [*ns_exec, "ip", "addr", "add", server_cidr, "dev", self._server_name],
            ["ip", "link", "set", self._client_name, "up"],
            [*ns_exec, "ip", "link", "set", self._server_name, "up"],
            # 本机地址（含 veth 端点 IP）的投递走 local 路由表、经 lo 回环；
            # 全新 net namespace（如 unshare -rn）中 lo 默认 DOWN，不启用
            # 则客户端连接本命名空间内的 server 地址直接超时。
            [*ns_exec, "ip", "link", "set", "lo", "up"],
        ]
        for cmd in commands:
            result = _run(cmd)
            if result.returncode != 0:
                raise RuntimeError(f"配置 veth 失败 ({' '.join(cmd)}): {result.stderr.strip()}")
