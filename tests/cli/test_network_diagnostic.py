"""网络诊断：本机 TCP、DNS、命令工具与超时回收。"""

from __future__ import annotations

import asyncio
import shutil
import socket

import pytest

from cli import network_diagnostic as net
from cli.main import WindHubShell


async def _tcp_server() -> tuple[asyncio.base_events.Server, int]:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1] if server.sockets else 0
    return server, port


def test_tcp_probe_success_against_local_server() -> None:
    async def scenario() -> dict:
        server, port = await _tcp_server()
        try:
            return await net.tcp("127.0.0.1", port, timeout=2)
        finally:
            server.close()

    result = asyncio.run(scenario())
    assert result["status"] == "PASS"
    assert result["port"] > 0
    assert result["elapsed_ms"] >= 0


def test_tcp_probe_refused_is_failure() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    shell = WindHubShell()
    result = shell.dispatch(["diagnose", "tcp", "127.0.0.1", str(free_port)])
    assert isinstance(result, dict)
    assert result["status"] == "FAIL"
    assert result["code"] == "NETWORK_PROBE_FAILED"


def test_tcp_probe_validates_port_and_timeout() -> None:
    with pytest.raises(ValueError):
        asyncio.run(net.tcp("127.0.0.1", 0))
    with pytest.raises(ValueError):
        asyncio.run(net.tcp("127.0.0.1", 80, timeout=0))


def test_tcp_probe_timeout_bounds_duration(monkeypatch) -> None:
    async def slow_open(host: str, port: int, **_: object) -> object:
        await asyncio.sleep(1.0)
        raise AssertionError("should be cancelled by timeout")

    monkeypatch.setattr(net.asyncio, "open_connection", slow_open)
    with pytest.raises(TimeoutError):
        asyncio.run(net.tcp("10.255.255.1", 9, timeout=0.05))


def test_resolve_localhost() -> None:
    result = asyncio.run(net.resolve("localhost", timeout=3))
    assert result["status"] == "PASS"
    assert result["addresses"]
    assert any(addr.startswith("127.") or "::1" in addr for addr in result["addresses"])


def test_route_get_returns_command_shape() -> None:
    if not shutil.which("ip"):
        pytest.skip("iproute2 not available")
    result = asyncio.run(net.route("127.0.0.1", timeout=3))
    assert "returncode" in result and "output" in result


def test_ping_validation_and_command_recycling(monkeypatch) -> None:
    with pytest.raises(ValueError):
        asyncio.run(net.ping("127.0.0.1", count=0))

    created: dict[str, object] = {}

    class FakeProcess:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError

        def kill(self) -> None:
            created["killed"] = True

        async def wait(self) -> None:
            self.returncode = -9

    async def fake_exec(*argv: object, **_: object) -> FakeProcess:
        created["argv"] = argv
        return FakeProcess()

    monkeypatch.setattr(net.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(TimeoutError):
        asyncio.run(net._command(["sleep", "60"], timeout=0.05))
    assert created.get("killed") is True


@pytest.mark.skipif(not shutil.which("ping"), reason="ping binary missing")
def test_ping_real_localhost_shape() -> None:
    result = asyncio.run(net.ping("127.0.0.1", count=1, timeout=2))
    assert result["host"] == "127.0.0.1"
    assert result["returncode"] in (0, 1, 2)


def test_tcp_uses_device_endpoint_from_config(config_dir) -> None:
    shell = WindHubShell()
    shell.config.read(config_dir("modbus"))
    result = shell.dispatch(["diagnose", "tcp", "modbus-001"])
    assert isinstance(result, dict)
    # Modbus 示例端口本机通常无监听：必须以带 endpoint 的 FAIL 信封返回，
    # 而不是 ValueError。
    assert result["status"] == "FAIL"
    assert result["code"] == "NETWORK_PROBE_FAILED"
    assert "host" in result and "port" in result
