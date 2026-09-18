"""Unit tests for ``cli/probe/local_info.py`` — 本机信息获取与网段判断。

``ip -j`` 子进程通过 monkeypatch 模块级 :func:`_run_ip_json` 替换，
不真的执行系统命令。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from wind_hub.adapter.inbound.cli.probe import local_info
from wind_hub.domain.model.errors import ConfigError

_ADDR_JSON: list[dict[str, Any]] = [
    {
        "ifname": "lo",
        "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}],
    },
    {
        "ifname": "eth0",
        "addr_info": [
            {"family": "inet", "local": "192.168.1.100", "prefixlen": 24},
            {"family": "inet6", "local": "fe80::1", "prefixlen": 64},
        ],
    },
    {
        "ifname": "docker0",
        "addr_info": [{"family": "inet", "local": "172.17.0.2", "prefixlen": 16}],
    },
]

_ROUTE_JSON: list[dict[str, Any]] = [
    {"dst": "default", "gateway": "192.168.1.1", "dev": "eth0"},
]


def _patch_ip(
    monkeypatch: pytest.MonkeyPatch,
    addr: list[dict[str, Any]] | None = None,
    route: list[dict[str, Any]] | None = None,
) -> None:
    """按子命令参数返回预置 JSON。"""

    async def _fake(*args: str) -> list[dict[str, Any]]:
        if args[:1] == ("addr",):
            return _ADDR_JSON if addr is None else addr
        if args[:1] == ("route",):
            return _ROUTE_JSON if route is None else route
        raise AssertionError(f"unexpected ip args: {args}")

    monkeypatch.setattr(local_info, "_run_ip_json", _fake)


async def test_get_local_info_parses_addr_and_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过滤回环接口与 inet6；网关取第一条默认路由。"""
    _patch_ip(monkeypatch)
    info = await local_info.get_local_info()
    assert info.hostname  # 真实 hostname，非空即可
    assert info.ips == [("192.168.1.100/24", "eth0"), ("172.17.0.2/16", "docker0")]
    assert info.default_gateway == "192.168.1.1"


async def test_get_local_info_no_default_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ip(monkeypatch, route=[])
    info = await local_info.get_local_info()
    assert info.default_gateway is None


async def test_get_local_info_no_gateway_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ip(monkeypatch, route=[{"dst": "default", "dev": "eth0"}])
    info = await local_info.get_local_info()
    assert info.default_gateway is None


async def test_get_local_info_non_linux_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ConfigError, match="only supports Linux"):
        await local_info.get_local_info()


def test_is_same_subnet_match() -> None:
    same, desc = local_info.is_same_subnet(
        [("192.168.1.100/24", "eth0"), ("172.17.0.2/16", "docker0")],
        "192.168.1.55",
    )
    assert same is True
    assert desc == "192.168.1.0/24 (eth0)"


def test_is_same_subnet_match_second_interface() -> None:
    same, desc = local_info.is_same_subnet(
        [("192.168.1.100/24", "eth0"), ("172.17.0.2/16", "docker0")],
        "172.17.9.9",
    )
    assert same is True
    assert desc == "172.17.0.0/16 (docker0)"


def test_is_same_subnet_no_match() -> None:
    same, desc = local_info.is_same_subnet([("192.168.1.100/24", "eth0")], "10.0.1.1")
    assert same is False
    assert desc is None


def test_is_same_subnet_invalid_target_raises() -> None:
    with pytest.raises(ConfigError, match="不是合法 IPv4"):
        local_info.is_same_subnet([("192.168.1.100/24", "eth0")], "not-an-ip")


def test_is_same_subnet_skips_unparseable_local_entry() -> None:
    """本机条目异常时跳过而不是整批失败（防御性容错）。"""
    same, _desc = local_info.is_same_subnet(
        [("bad-entry", "eth0"), ("10.0.1.5/24", "eth1")], "10.0.1.1"
    )
    assert same is True
