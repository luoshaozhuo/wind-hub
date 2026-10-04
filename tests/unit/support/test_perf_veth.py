"""Unit tests for ``tests/perf/veth.py`` — veth pair 管理。

``_run`` 与 ``os.geteuid`` 全部 monkeypatch，不执行真实 ``ip`` 命令。
"""

from __future__ import annotations

import subprocess

import pytest

from tests.performance import veth as veth_mod
from tests.performance.veth import VethManager


class _FakeIp:
    """按内存状态模拟 ``ip`` 命令的假执行器。"""

    def __init__(self) -> None:
        self.links: set[str] = set()
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:3] == ["ip", "link", "show"]:
            rc = 0 if args[3] in self.links else 1
            return subprocess.CompletedProcess(args, rc, "", "" if rc == 0 else "not found")
        if args[:3] == ["ip", "link", "add"]:
            # ip link add veth-wc type veth peer name veth-ws
            self.links.add(args[3])
            self.links.add(args[8])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["ip", "link", "del"]:
            self.links.discard(args[3])
            # 删除一端即删除整对
            self.links.discard("veth-ws" if args[3] == "veth-wc" else "veth-wc")
            return subprocess.CompletedProcess(args, 0, "", "")
        # addr add / link set up
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def fake_ip(monkeypatch: pytest.MonkeyPatch) -> _FakeIp:
    fake = _FakeIp()
    monkeypatch.setattr(veth_mod, "_run", fake)
    monkeypatch.setattr(veth_mod.os, "geteuid", lambda: 0)
    return fake


def _as_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(veth_mod.os, "geteuid", lambda: 1000)


def test_check_permission_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(veth_mod.os, "geteuid", lambda: 0)
    assert VethManager().check_permission() is True


def test_check_permission_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_non_root(monkeypatch)
    assert VethManager().check_permission() is False


def test_create_assigns_addresses_and_brings_up(fake_ip: _FakeIp) -> None:
    pair = VethManager().create()

    assert pair.name_client == "veth-wc"
    assert pair.name_server == "veth-ws"
    assert pair.ip_client == "10.99.0.1"
    assert pair.ip_server == "10.99.0.2"
    joined = [" ".join(c) for c in fake_ip.calls]
    assert "ip addr add 10.99.0.1/24 dev veth-wc" in joined
    assert "ip addr add 10.99.0.2/24 dev veth-ws" in joined
    assert "ip link set veth-wc up" in joined
    assert "ip link set veth-ws up" in joined
    # 本机地址投递经 lo 回环；全新 net namespace 中 lo 默认 DOWN，
    # 不启用则客户端无法连接本命名空间内的 veth 端点地址。
    assert "ip link set lo up" in joined


def test_create_with_server_namespace_moves_end_and_configures_via_netns(
    fake_ip: _FakeIp,
) -> None:
    """跨命名空间拓扑：server 端挪入 netns，配置命令经 ip netns exec 下达。

    同命名空间时本机投递走 lo 回环、绕过 veth qdisc，netem 不生效——
    reliability 网络降级测试依赖此拓扑让流量真实穿越 veth pair。
    """
    pair = VethManager(server_namespace="whn-test").create()

    assert pair.name_server == "veth-ws"
    joined = [" ".join(c) for c in fake_ip.calls]
    assert "ip link set veth-ws netns whn-test" in joined
    assert "ip netns exec whn-test ip addr add 10.99.0.2/24 dev veth-ws" in joined
    assert "ip netns exec whn-test ip link set veth-ws up" in joined
    assert "ip netns exec whn-test ip link set lo up" in joined
    # client 端仍在 root namespace，不走 netns exec。
    assert "ip addr add 10.99.0.1/24 dev veth-wc" in joined


def test_create_is_idempotent(fake_ip: _FakeIp) -> None:
    mgr = VethManager()
    mgr.create()
    calls_after_first = len(fake_ip.calls)
    pair = mgr.create()

    assert pair.ip_server == "10.99.0.2"
    # 第二次 create 只查了一次 exists，不再执行任何变更命令
    assert len(fake_ip.calls) == calls_after_first + 1


def test_destroy_is_idempotent(fake_ip: _FakeIp) -> None:
    mgr = VethManager()
    mgr.destroy()  # 不存在时不报错
    mgr.create()
    mgr.destroy()
    assert not mgr.exists()
    mgr.destroy()  # 再次删除同样不报错


def test_create_requires_root(monkeypatch: pytest.MonkeyPatch, fake_ip: _FakeIp) -> None:
    _as_non_root(monkeypatch)
    with pytest.raises(PermissionError, match="root"):
        VethManager().create()


def test_create_failure_cleans_up_half_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """addr 分配失败时，已创建的 link 必须被清理（create 可重入）。"""
    fake = _FakeIp()
    real_call = fake.__call__

    def _flaky(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["ip", "addr", "add"]:
            return subprocess.CompletedProcess(args, 1, "", "RTNETLINK error")
        return real_call(args)

    monkeypatch.setattr(veth_mod, "_run", _flaky)
    monkeypatch.setattr(veth_mod.os, "geteuid", lambda: 0)

    mgr = VethManager()
    with pytest.raises(RuntimeError, match="配置 veth 失败"):
        mgr.create()
    assert not mgr.exists()  # 半截状态已清理


async def test_active_context_manager(fake_ip: _FakeIp) -> None:
    mgr = VethManager()
    async with mgr.active() as pair:
        assert mgr.exists()
        assert pair.ip_client == "10.99.0.1"
    assert not mgr.exists()  # 退出即销毁


async def test_active_cleans_up_on_exception(fake_ip: _FakeIp) -> None:
    mgr = VethManager()
    with pytest.raises(RuntimeError, match="boom"):
        async with mgr.active():
            raise RuntimeError("boom")
    assert not mgr.exists()
