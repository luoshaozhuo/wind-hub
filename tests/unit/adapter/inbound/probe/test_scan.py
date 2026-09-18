"""Unit tests for ``cli/probe/scan.py`` — scan_network 组合编排。

三种扫描方法全部 monkeypatch 替换，验证合并去重、方法开关、降级与
主机名反查的编排逻辑。
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest

from wind_hub.adapter.inbound.cli.probe import scan
from wind_hub.domain.model.errors import ConfigError


@pytest.fixture(autouse=True)
def _mock_methods(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """默认：ARP 有 .1，ICMP 有 .1/.2，TCP 有 .2/.3；ping 可用。"""
    state: dict[str, Any] = {"calls": []}

    async def _arp(network: str) -> dict[str, str]:
        state["calls"].append("arp")
        return {"10.0.1.1": "aa:bb:cc:dd:ee:01"}

    async def _icmp(ips: list[str], timeout: float, concurrency: int) -> set[str]:
        state["calls"].append("icmp")
        return {"10.0.1.1", "10.0.1.2"}

    async def _tcp(ips: list[str], ports: Any, timeout: float, concurrency: int) -> set[str]:
        state["calls"].append("tcp")
        return {"10.0.1.2", "10.0.1.3"}

    monkeypatch.setattr(scan, "scan_arp", _arp)
    monkeypatch.setattr(scan, "scan_icmp", _icmp)
    monkeypatch.setattr(scan, "scan_tcp", _tcp)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/sbin/ping" if cmd == "ping" else None)
    return state


async def test_scan_network_merges_and_dedupes(_mock_methods: dict[str, Any]) -> None:
    results = await scan.scan_network("10.0.1.0/29")
    by_ip = {r.ip: r for r in results}
    assert set(by_ip) == {"10.0.1.1", "10.0.1.2", "10.0.1.3"}
    # detected_by 按 arp → icmp → tcp 顺序累积
    assert by_ip["10.0.1.1"].detected_by == ["arp", "icmp"]
    assert by_ip["10.0.1.1"].mac == "aa:bb:cc:dd:ee:01"  # MAC 只来自 ARP
    assert by_ip["10.0.1.2"].detected_by == ["icmp", "tcp"]
    assert by_ip["10.0.1.2"].mac is None
    assert by_ip["10.0.1.3"].detected_by == ["tcp"]
    assert all(r.alive for r in results)
    # 按 IP 数值排序
    assert [r.ip for r in results] == ["10.0.1.1", "10.0.1.2", "10.0.1.3"]


async def test_scan_network_disable_icmp(_mock_methods: dict[str, Any]) -> None:
    results = await scan.scan_network("10.0.1.0/29", use_icmp=False)
    assert "icmp" not in _mock_methods["calls"]
    by_ip = {r.ip: r for r in results}
    assert by_ip["10.0.1.1"].detected_by == ["arp"]
    assert by_ip["10.0.1.2"].detected_by == ["tcp"]


async def test_scan_network_disable_tcp(_mock_methods: dict[str, Any]) -> None:
    results = await scan.scan_network("10.0.1.0/29", use_tcp=False)
    assert "tcp" not in _mock_methods["calls"]
    assert {r.ip for r in results} == {"10.0.1.1", "10.0.1.2"}


async def test_scan_network_degrades_when_ping_missing(
    monkeypatch: pytest.MonkeyPatch, _mock_methods: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """决策 6：ping 二进制缺失 → 跳过 ICMP、记日志、TCP 仍兜底。"""
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    import logging

    with caplog.at_level(logging.WARNING, logger=scan.__name__):
        results = await scan.scan_network("10.0.1.0/29")
    assert "icmp" not in _mock_methods["calls"]
    assert "tcp" in _mock_methods["calls"]
    assert any("ping" in r.getMessage() for r in caplog.records)
    # ICMP 缺席时 arp(.1) + tcp(.2/.3) 仍覆盖全部三个存活地址
    assert {r.ip for r in results} == {"10.0.1.1", "10.0.1.2", "10.0.1.3"}


async def test_scan_network_resolve_hostname(
    monkeypatch: pytest.MonkeyPatch, _mock_methods: dict[str, Any]
) -> None:
    async def _resolve(ip: str, timeout: float = 0.5) -> str | None:
        return "plc-001" if ip == "10.0.1.1" else None

    monkeypatch.setattr(scan, "_resolve_hostname", _resolve)
    results = await scan.scan_network("10.0.1.0/29", resolve_hostname=True)
    by_ip = {r.ip: r for r in results}
    assert by_ip["10.0.1.1"].hostname == "plc-001"
    assert by_ip["10.0.1.2"].hostname is None


async def test_scan_network_default_no_hostname(_mock_methods: dict[str, Any]) -> None:
    results = await scan.scan_network("10.0.1.0/29")
    assert all(r.hostname is None for r in results)


async def test_scan_network_rereads_arp_after_icmp(
    monkeypatch: pytest.MonkeyPatch, _mock_methods: dict[str, Any]
) -> None:
    """决策 0.1：ICMP 后重读 ARP 表——ping 新解析出的邻居补齐 MAC，
    仅在新 ARP 表里出现的地址也并入结果。"""
    arp_calls: list[str] = []

    async def _arp_staged(network: str) -> dict[str, str]:
        arp_calls.append(network)
        if len(arp_calls) == 1:
            return {"10.0.1.1": "aa:bb:cc:dd:ee:01"}
        # ping 之后：.2 解析出了 MAC，.4 也出现在邻居缓存里
        return {
            "10.0.1.1": "aa:bb:cc:dd:ee:01",
            "10.0.1.2": "aa:bb:cc:dd:ee:02",
            "10.0.1.4": "aa:bb:cc:dd:ee:04",
        }

    monkeypatch.setattr(scan, "scan_arp", _arp_staged)
    results = await scan.scan_network("10.0.1.0/29")

    assert len(arp_calls) == 2  # ICMP 后重读一次
    by_ip = {r.ip: r for r in results}
    assert by_ip["10.0.1.2"].mac == "aa:bb:cc:dd:ee:02"  # MAC 补齐
    assert by_ip["10.0.1.2"].detected_by == ["icmp", "tcp"]  # detected_by 不变
    assert by_ip["10.0.1.4"].detected_by == ["arp"]  # 新 ARP 条目并入
    assert by_ip["10.0.1.4"].mac == "aa:bb:cc:dd:ee:04"


async def test_scan_network_no_arp_reread_without_icmp(
    monkeypatch: pytest.MonkeyPatch, _mock_methods: dict[str, Any]
) -> None:
    """ICMP 禁用时不重读 ARP（没有新解析发生，重读无意义）。"""
    arp_calls = 0

    async def _arp_counting(network: str) -> dict[str, str]:
        nonlocal arp_calls
        arp_calls += 1
        return {"10.0.1.1": "aa:bb:cc:dd:ee:01"}

    monkeypatch.setattr(scan, "scan_arp", _arp_counting)
    await scan.scan_network("10.0.1.0/29", use_icmp=False)
    assert arp_calls == 1


async def test_scan_network_non_linux_raises(
    monkeypatch: pytest.MonkeyPatch, _mock_methods: dict[str, Any]
) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ConfigError, match="only supports Linux"):
        await scan.scan_network("10.0.1.0/29")


async def test_scan_network_invalid_network_raises(_mock_methods: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        await scan.scan_network("not-a-network")


async def test_resolve_hostname_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    class _Loop:
        async def getnameinfo(self, sockaddr: Any, flags: int) -> tuple[str, list, list]:
            await asyncio.sleep(10)
            return "x", [], []

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _Loop())
    assert await scan._resolve_hostname("10.0.1.1", timeout=0.01) is None  # noqa: SLF001
