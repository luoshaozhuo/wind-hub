"""Unit tests for ``cli/probe/scan_methods.py`` — ARP / ICMP / TCP 三种方法。

所有系统交互（/proc/net/arp、ping 子进程、TCP connect）都通过
monkeypatch 替换，不触网、不需要 root。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from wind_hub.adapter.inbound.cli.probe import scan_methods
from wind_hub.domain.model.errors import ConfigError

# ---------------------------------------------------------------------------
# 平台检查（决策 1）
# ---------------------------------------------------------------------------


def test_check_linux_passes_on_linux() -> None:
    if sys.platform == "linux":
        scan_methods.check_linux()  # 不抛异常


def test_check_linux_raises_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ConfigError, match="only supports Linux"):
        scan_methods.check_linux()


# ---------------------------------------------------------------------------
# WSL 检测（决策 0.2）
# ---------------------------------------------------------------------------


def test_is_wsl_detects_microsoft_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version = tmp_path / "version"
    version.write_text(
        "Linux version 5.15.90.1-microsoft-standard-WSL2 (gcc) #1 SMP\n", encoding="ascii"
    )
    monkeypatch.setattr(scan_methods, "_PROC_VERSION", version)
    assert scan_methods.is_wsl() is True


def test_is_wsl_false_on_native_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version = tmp_path / "version"
    version.write_text("Linux version 6.8.0-31-generic (gcc) #31-Ubuntu SMP\n", encoding="ascii")
    monkeypatch.setattr(scan_methods, "_PROC_VERSION", version)
    assert scan_methods.is_wsl() is False


def test_is_wsl_false_when_version_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_methods, "_PROC_VERSION", tmp_path / "nonexistent")
    assert scan_methods.is_wsl() is False


# ---------------------------------------------------------------------------
# ARP 表解析（决策 3）
# ---------------------------------------------------------------------------

_ARP_TABLE = """\
IP address       HW type     Flags       HW address            Mask     Device
10.0.1.1         0x1         0x2         aa:bb:cc:dd:ee:01     *        eth0
10.0.1.2         0x1         0x0         00:00:00:00:00:00     *        eth0
10.0.2.7         0x1         0x2         aa:bb:cc:dd:ee:07     *        eth0
192.168.1.1      0x1         0x2         11:22:33:44:55:66     *        eth0
"""


def test_parse_arp_table_keeps_only_complete_entries() -> None:
    entries = scan_methods._parse_arp_table(_ARP_TABLE)  # noqa: SLF001
    assert entries == {
        "10.0.1.1": "aa:bb:cc:dd:ee:01",
        "10.0.2.7": "aa:bb:cc:dd:ee:07",
        "192.168.1.1": "11:22:33:44:55:66",
    }


async def test_scan_arp_filters_to_target_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arp_file = tmp_path / "arp"
    arp_file.write_text(_ARP_TABLE, encoding="ascii")
    monkeypatch.setattr(scan_methods, "_PROC_NET_ARP", arp_file)

    entries = await scan_methods.scan_arp("10.0.1.0/29")
    # 10.0.1.2 是 Flags=0x0 的不完整条目被过滤；10.0.2.7 / 192.168.1.1 不在网段内
    assert entries == {"10.0.1.1": "aa:bb:cc:dd:ee:01"}


async def test_scan_arp_falls_back_to_ip_neigh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_methods, "_PROC_NET_ARP", tmp_path / "nonexistent")

    neigh_output = (
        b"10.0.1.3 dev eth0 lladdr aa:bb:cc:dd:ee:03 REACHABLE\n" b"10.0.1.4 dev eth0 FAILED\n"
    )

    class _Proc:
        async def communicate(self) -> tuple[bytes, bytes]:
            return neigh_output, b""

    async def _fake_exec(*args: Any, **kwargs: Any) -> _Proc:
        assert args[:3] == ("ip", "neigh", "show")
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    entries = await scan_methods.scan_arp("10.0.1.0/29")
    assert entries == {"10.0.1.3": "aa:bb:cc:dd:ee:03"}


# ---------------------------------------------------------------------------
# ICMP（决策 4）
# ---------------------------------------------------------------------------


class _FakePingProc:
    def __init__(self, returncode: int) -> None:
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


def _patch_ping(monkeypatch: pytest.MonkeyPatch, alive: set[str]) -> list[list[str]]:
    """替换 asyncio.create_subprocess_exec：alive 集合内的 IP 返回退出码 0。"""
    calls: list[list[str]] = []

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakePingProc:
        calls.append(list(args))
        ip = str(args[-1])
        return _FakePingProc(0 if ip in alive else 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return calls


async def test_scan_icmp_returns_alive_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_ping(monkeypatch, alive={"10.0.1.1", "10.0.1.3"})
    result = await scan_methods.scan_icmp(
        ["10.0.1.1", "10.0.1.2", "10.0.1.3"], timeout=0.1, concurrency=4
    )
    assert result == {"10.0.1.1", "10.0.1.3"}
    # 用系统 ping，单发单超时
    assert calls[0][:3] == ["ping", "-c", "1"]


async def test_scan_icmp_missing_ping_binary_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_ping(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("ping")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_ping)
    result = await scan_methods.scan_icmp(["10.0.1.1"], timeout=0.1, concurrency=4)
    assert result == set()


# ---------------------------------------------------------------------------
# TCP connect 兜底（决策 5）
# ---------------------------------------------------------------------------


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _patch_open_connection(
    monkeypatch: pytest.MonkeyPatch, open_ports: dict[str, set[int]]
) -> None:
    """open_ports[ip] 内的端口 connect 成功，其余 ConnectionRefusedError。"""

    async def _fake_open(ip: str, port: int) -> tuple[None, _FakeWriter]:
        if port in open_ports.get(ip, set()):
            return None, _FakeWriter()
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(asyncio, "open_connection", _fake_open)


async def test_scan_tcp_alive_when_any_port_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_open_connection(monkeypatch, {"10.0.1.1": {502}, "10.0.1.2": {2404}})
    result = await scan_methods.scan_tcp(
        ["10.0.1.1", "10.0.1.2", "10.0.1.3"],
        [502, 2404],
        timeout=0.1,
        concurrency=4,
    )
    assert result == {"10.0.1.1", "10.0.1.2"}


async def test_scan_tcp_timeout_counts_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hang(ip: str, port: int) -> Any:
        await asyncio.sleep(10)
        return None, _FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", _hang)
    result = await scan_methods.scan_tcp(["10.0.1.1"], [502], timeout=0.01, concurrency=4)
    assert result == set()
