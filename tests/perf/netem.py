"""tc netem 封装（决策 2/3）——网络损伤注入。

一个 :class:`NetemController` 管一个网络接口上的 netem qdisc；压测时
对 veth pair **两端各挂一个** controller（双方向都受损伤，RTT 语义才
与「基站链路延迟」一致：请求去程 + 响应回程各承担一半在直觉上对称）。

中断场景（``outage_duration_s > 0``）不走 :meth:`apply`，而是在测量阶段
由 :meth:`simulate_outage` 触发「100% 丢包 → 等待 → 恢复」。

需要 root（CAP_NET_ADMIN）；**不自动 sudo**（决策 10）。
所有命令经 :func:`_run` 执行（模块级 seam，单测可替换）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class NetemScenario:
    """一个网络场景。

    全部为零 = 理想链路。``outage_duration_s > 0`` 表示中断场景：
    不静态应用规则，由 controller 在测量中触发一次中断-恢复。
    """

    name: str
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    outage_duration_s: float = 0.0

    @property
    def is_ideal(self) -> bool:
        """无需注入任何规则的「理想链路」场景。"""
        return (
            self.delay_ms == 0.0
            and self.jitter_ms == 0.0
            and self.loss_pct == 0.0
            and self.outage_duration_s == 0.0
        )


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """执行一条系统命令（不 check，由调用方按返回码判定）。"""
    return subprocess.run(args, capture_output=True, text=True, check=False)


class NetemController:
    """单个网络接口上的 tc netem 控制器。"""

    def __init__(self, device: str) -> None:
        self._device = device
        self._active = False

    @property
    def device(self) -> str:
        return self._device

    def check_permission(self) -> bool:
        """root（CAP_NET_ADMIN）判定。"""
        return os.geteuid() == 0

    def apply(self, scenario: NetemScenario) -> None:
        """应用 netem 规则（幂等：用 ``replace`` 覆盖已有 qdisc）。

        理想场景等价于 :meth:`clear`（不挂任何规则，零开销）。
        中断场景（``outage_duration_s``）不在此处理——它没有时间稳态，
        由 :meth:`simulate_outage` 在测量阶段触发。

        Raises:
            PermissionError: 非 root。
            RuntimeError: ``tc`` 命令执行失败。
        """
        if not self.check_permission():
            raise PermissionError("tc netem 需要 root 权限（决策 10：不自动 sudo）")
        if scenario.is_ideal or scenario.outage_duration_s > 0:
            self.clear()
            return

        cmd = ["tc", "qdisc", "replace", "dev", self._device, "root", "netem"]
        if scenario.delay_ms > 0:
            cmd.append(f"delay {scenario.delay_ms:g}ms")
            if scenario.jitter_ms > 0:
                cmd.append(f"{scenario.jitter_ms:g}ms")
        if scenario.loss_pct > 0:
            cmd.append(f"loss {scenario.loss_pct:g}%")
        result = _run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"应用 netem 失败 ({' '.join(cmd)}): {result.stderr.strip()}")
        self._active = True

    def clear(self) -> None:
        """清除 netem 规则（幂等：接口上没有 qdisc 也视为成功）。"""
        if not self._active:
            return
        # 删除失败（如接口已消失）只意味着「规则已不在」，语义上等同清除。
        _run(["tc", "qdisc", "del", "dev", self._device, "root"])
        self._active = False

    @asynccontextmanager
    async def scenario(self, s: NetemScenario) -> AsyncIterator[None]:
        """上下文管理器：应用 → yield → 清除（异常路径也保证清除）。"""
        self.apply(s)
        try:
            yield
        finally:
            self.clear()

    async def simulate_outage(self, duration_s: float) -> None:
        """模拟一次链路中断：100% 丢包 → 等 ``duration_s`` → 恢复（清除）。

        Raises:
            PermissionError: 非 root。
            RuntimeError: ``tc`` 命令执行失败。
        """
        if not self.check_permission():
            raise PermissionError("tc netem 需要 root 权限（决策 10：不自动 sudo）")
        result = _run(["tc", "qdisc", "replace", "dev", self._device, "root", "netem", "loss 100%"])
        if result.returncode != 0:
            raise RuntimeError(f"注入中断失败: {result.stderr.strip()}")
        self._active = True
        await asyncio.sleep(duration_s)
        self.clear()
