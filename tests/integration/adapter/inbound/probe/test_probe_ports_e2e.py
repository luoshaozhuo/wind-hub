"""Integration tests for ``wind-hub probe ports``（CLI 端到端）。

回环真实 server 用原始 listening socket 实现（同一进程内对任何事件
循环可见，accept 与否不影响 TCP connect 成功判定）；只绑定
127.0.0.1，不触碰真实外部 IP（决策 11）。
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.adapter.inbound.cli.probe.ports import scan_ports
from wind_hub.adapter.inbound.cli.probe.ports_models import PortScanResult, PortState
from wind_hub.config.ports_config import default_ports_config, load_ports_config


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def listening_socket() -> Iterator[socket.socket]:
    """回环随机端口的真实监听 socket（CLI 的 asyncio.run 也能连通）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    yield srv
    srv.close()


def _unused_port() -> int:
    """取一个当前未监听的回环端口（绑定后立即释放）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# 真实回环扫描（不经 CLI）
# ---------------------------------------------------------------------------


async def test_scan_ports_real_loopback_open_and_closed(
    listening_socket: socket.socket,
) -> None:
    open_port = listening_socket.getsockname()[1]
    closed_port = _unused_port()
    result = await scan_ports("127.0.0.1", [open_port, closed_port], timeout=1.0, concurrency=4)
    states = {p.port: p.state for p in result.ports}
    assert states[open_port] is PortState.OPEN
    assert states[closed_port] is PortState.CLOSED
    assert result.open_count == 1


# ---------------------------------------------------------------------------
# CLI 端到端
# ---------------------------------------------------------------------------


def test_cli_scan_open_port_table(runner: CliRunner, listening_socket: socket.socket) -> None:
    port = listening_socket.getsockname()[1]
    result = runner.invoke(
        build_cli(),
        ["probe", "ports", "--host", "127.0.0.1", "--ports", str(port), "--timeout", "2"],
    )
    assert result.exit_code == 0
    assert str(port) in result.output
    assert "open" in result.output
    assert "共扫描 1 个端口，开放 1 个" in result.output


def test_cli_scan_closed_port_table(runner: CliRunner) -> None:
    port = _unused_port()
    result = runner.invoke(
        build_cli(),
        ["probe", "ports", "--host", "127.0.0.1", "--ports", str(port), "--timeout", "2"],
    )
    assert result.exit_code == 0
    assert "closed" in result.output
    assert "共扫描 1 个端口，开放 0 个" in result.output


def test_cli_default_ports_when_unspecified(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--ports 缺省时用端口配置的默认端口集（不真扫——替换 scan_ports 验证传参）。

    默认端口集为端口配置 mapping 的全部端口（pytest 从仓库根运行，
    CLI 缺省的 --ports-config 路径命中真实配置文件）。
    """
    captured: dict[str, object] = {}

    async def _fake(
        ip: str, ports: list[int], timeout: float, concurrency: int, config: object = None
    ) -> object:
        captured["ports"] = ports
        captured["timeout"] = timeout
        captured["concurrency"] = concurrency
        return PortScanResult(ip=ip, ports=[], total=len(ports), open_count=0)

    monkeypatch.setattr("wind_hub.adapter.inbound.cli.commands.probe_ports.scan_ports", _fake)
    result = runner.invoke(build_cli(), ["probe", "ports", "--host", "127.0.0.1"])
    assert result.exit_code == 0
    expected = load_ports_config("configs/ports.yaml")
    assert captured["ports"] == list(expected.mapping)
    assert 502 in captured["ports"]  # type: ignore[operator]
    assert 80 in captured["ports"]  # type: ignore[operator]
    # 缺省 timeout/concurrency 也来自配置文件
    assert captured["timeout"] == expected.timeout
    assert captured["concurrency"] == expected.concurrency


def test_cli_default_ports_builtin_fallback_without_config(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--ports-config 指向不存在文件时回落内置工业协议映射的端口集。"""
    captured: dict[str, object] = {}

    async def _fake(
        ip: str, ports: list[int], timeout: float, concurrency: int, config: object = None
    ) -> object:
        captured["ports"] = ports
        return PortScanResult(ip=ip, ports=[], total=len(ports), open_count=0)

    monkeypatch.setattr("wind_hub.adapter.inbound.cli.commands.probe_ports.scan_ports", _fake)
    result = runner.invoke(
        build_cli(),
        [
            "probe",
            "ports",
            "--host",
            "127.0.0.1",
            "--ports-config",
            str(tmp_path / "nonexistent.yaml"),
        ],
    )
    assert result.exit_code == 0
    assert captured["ports"] == list(default_ports_config().mapping)


def test_cli_json_output(runner: CliRunner, listening_socket: socket.socket) -> None:
    port = listening_socket.getsockname()[1]
    result = runner.invoke(
        build_cli(),
        ["probe", "ports", "--host", "127.0.0.1", "--ports", str(port), "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ip"] == "127.0.0.1"
    assert payload["total"] == 1
    assert payload["open_count"] == 1
    assert payload["ports"][0] == {"port": port, "state": "open", "service_guess": None}


def test_cli_yaml_output(runner: CliRunner) -> None:
    port = _unused_port()
    result = runner.invoke(
        build_cli(),
        ["probe", "ports", "--host", "127.0.0.1", "--ports", str(port), "--yaml"],
    )
    assert result.exit_code == 0
    payload = yaml.safe_load(result.output)
    assert payload["ports"][0]["state"] == "closed"


def test_cli_invalid_host_exit_one(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "ports", "--host", "not-an-ip", "--ports", "502"])
    assert result.exit_code == 1
    assert "端口扫描失败" in result.output


def test_cli_invalid_ports_exit_one(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "ports", "--host", "127.0.0.1", "--ports", "abc"])
    assert result.exit_code == 1


def test_cli_json_and_yaml_conflict(runner: CliRunner) -> None:
    result = runner.invoke(
        build_cli(), ["probe", "ports", "--host", "127.0.0.1", "--json", "--yaml"]
    )
    assert result.exit_code == 1
    assert "二选一" in result.output


def test_probe_ports_help(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["probe", "ports", "--help"])
    assert result.exit_code == 0
    for opt in [
        "--host",
        "--ports",
        "--timeout",
        "--concurrency",
        "--ports-config",
        "--json",
        "--yaml",
    ]:
        assert opt in result.output
