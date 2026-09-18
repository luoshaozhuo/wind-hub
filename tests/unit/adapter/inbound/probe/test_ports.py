"""Unit tests for ``cli/probe/ports.py`` — TCP 端口扫描三态。

``asyncio.open_connection`` 全部 monkeypatch 替换，不触网。
"""

from __future__ import annotations

import asyncio
import errno
import sys
from typing import Any

import pytest

from wind_hub.adapter.inbound.cli.probe import ports as ports_mod
from wind_hub.adapter.inbound.cli.probe.ports_models import PortState
from wind_hub.domain.model.errors import ConfigError


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _patch_open(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> None:
    """behavior: 异常实例 / "open" / callable(ip, port)."""

    async def _fake(ip: str, port: int) -> tuple[None, _FakeWriter]:
        if callable(behavior):
            return await behavior(ip, port)
        if behavior == "open":
            return None, _FakeWriter()
        raise behavior

    monkeypatch.setattr(asyncio, "open_connection", _fake)


# ---------------------------------------------------------------------------
# scan_port 三态（决策 4）
# ---------------------------------------------------------------------------


async def test_scan_port_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_open(monkeypatch, "open")
    result = await ports_mod.scan_port("10.0.1.1", 502, timeout=0.1)
    assert result.state is PortState.OPEN
    assert result.service_guess == "modbus"


async def test_scan_port_closed_on_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_open(monkeypatch, ConnectionRefusedError("refused"))
    result = await ports_mod.scan_port("10.0.1.1", 2404, timeout=0.1)
    assert result.state is PortState.CLOSED
    assert result.service_guess == "iec104"


async def test_scan_port_timeout_on_no_response(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hang(ip: str, port: int) -> Any:
        await asyncio.sleep(10)
        return None, _FakeWriter()

    _patch_open(monkeypatch, _hang)
    result = await ports_mod.scan_port("10.0.1.1", 48898, timeout=0.01)
    assert result.state is PortState.TIMEOUT


async def test_scan_port_other_oserror_conservatively_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接重置等其他 OSError → 保守归 TIMEOUT（决策 4）。"""
    _patch_open(monkeypatch, ConnectionResetError("reset by peer"))
    result = await ports_mod.scan_port("10.0.1.1", 502, timeout=0.1)
    assert result.state is PortState.TIMEOUT


@pytest.mark.parametrize(
    "err_no",
    [errno.EHOSTUNREACH, errno.ENETUNREACH],
)
async def test_scan_port_unreachable_on_route_errors(
    monkeypatch: pytest.MonkeyPatch, err_no: int
) -> None:
    """EHOSTUNREACH / ENETUNREACH → UNREACHABLE（step20 任务 0.1：路由不存在）。"""
    _patch_open(monkeypatch, OSError(err_no, "No route to host"))
    result = await ports_mod.scan_port("10.0.1.1", 502, timeout=0.1)
    assert result.state is PortState.UNREACHABLE


# ---------------------------------------------------------------------------
# scan_ports 并发与排序
# ---------------------------------------------------------------------------


async def test_scan_ports_mixed_states_sorted_by_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _behavior(ip: str, port: int) -> tuple[None, _FakeWriter]:
        if port == 502:
            return None, _FakeWriter()
        if port == 2404:
            raise ConnectionRefusedError("refused")
        await asyncio.sleep(10)
        return None, _FakeWriter()

    _patch_open(monkeypatch, _behavior)
    result = await ports_mod.scan_ports("10.0.1.1", [48898, 502, 2404], timeout=0.01, concurrency=4)
    assert [p.port for p in result.ports] == [502, 2404, 48898]  # 按端口排序
    assert [p.state for p in result.ports] == [
        PortState.OPEN,
        PortState.CLOSED,
        PortState.TIMEOUT,
    ]
    assert result.total == 3
    assert result.open_count == 1
    assert result.ip == "10.0.1.1"


async def test_scan_ports_respects_concurrency_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发上限：同时在飞的 connect 不超过 concurrency。"""
    in_flight = 0
    peak = 0

    async def _behavior(ip: str, port: int) -> tuple[None, _FakeWriter]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return None, _FakeWriter()

    _patch_open(monkeypatch, _behavior)
    await ports_mod.scan_ports("10.0.1.1", list(range(8000, 8010)), timeout=1.0, concurrency=3)
    assert peak <= 3


async def test_scan_ports_non_linux_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ConfigError, match="only supports Linux"):
        await ports_mod.scan_ports("10.0.1.1", [502], timeout=0.1)
