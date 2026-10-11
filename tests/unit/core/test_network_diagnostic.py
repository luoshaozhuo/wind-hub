"""Core 通用网络诊断测试。"""

from __future__ import annotations

import asyncio

from core.infrastructure.diagnostic.network import ping_host, tcp_port_open


async def test_tcp_port_open_and_closed() -> None:
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        socket = server.sockets[0]
        port = int(socket.getsockname()[1])
        assert await tcp_port_open("127.0.0.1", port, timeout=1.0)
    finally:
        server.close()
        await server.wait_closed()

    # Bound listener is gone; the same port must now reject connections.
    assert not await tcp_port_open("127.0.0.1", port, timeout=0.2)


async def test_ping_missing_executable(monkeypatch) -> None:
    async def missing_executable(*args, **kwargs):
        raise FileNotFoundError("ping")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_executable)
    assert not await ping_host("127.0.0.1", timeout=0.1)


async def test_ping_exit_status(monkeypatch) -> None:
    class FakeProcess:
        async def wait(self) -> int:
            return 1

    async def fake_spawn(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    assert not await ping_host("127.0.0.1", timeout=0.1)
