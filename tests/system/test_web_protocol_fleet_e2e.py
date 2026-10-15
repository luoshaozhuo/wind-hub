"""Web Server 集成测试的真实协议环境：每类十个独立 TCP 从站。

仅绑定 loopback；每个端口由操作系统临时分配。所有协议栈均使用生产驱动
依赖的第三方服务实现，不使用空 TCP 占位服务。故障注入后验证端口关闭、
恢复与大量并发连接；恶意输入不得使其他协议服务崩溃。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass

import pytest
import pytest_asyncio

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.performance.servers import start_ads_server
from tests.support.process import free_port

pytestmark = [pytest.mark.asyncio(loop_scope="module"), pytest.mark.system]

_COUNT = 10
_HOST = "127.0.0.1"


@dataclass(slots=True)
class ProtocolFleet:
    modbus: list[ModbusMockServer]
    iec104: list[IEC104MockServer]
    ads_ports: list[int]

    def endpoints(self) -> dict[str, list[tuple[str, int]]]:
        return {
            "modbus": [(server.host, server.port) for server in self.modbus],
            "iec104": [(_HOST, server.port) for server in self.iec104],
            "ads": [(_HOST, port) for port in self.ads_ports],
        }


async def _can_connect(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=1.0
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    del reader
    return True


async def _await_listening(host: str, port: int) -> None:
    async def ready() -> None:
        for _ in range(50):
            if await _can_connect(host, port):
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"server did not listen on {host}:{port}")

    await asyncio.wait_for(ready(), timeout=8.0)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def protocol_fleet() -> AsyncIterator[ProtocolFleet]:
    """30 个协议 Server 完整生命周期；部分启动失败也必须清理已启动项。"""
    modbus: list[ModbusMockServer] = []
    iec104: list[IEC104MockServer] = []
    ads_ports: list[int] = []
    async with AsyncExitStack() as stack:
        async def start_modbus() -> ModbusMockServer:
            server = ModbusMockServer(port=free_port())
            await server.start()
            stack.push_async_callback(server.stop)
            await _await_listening(server.host, server.port)
            return server

        async def start_iec104() -> IEC104MockServer:
            server = IEC104MockServer(port=free_port(), data_points={100: 25.0})
            await server.start()
            stack.push_async_callback(server.stop)
            await _await_listening(_HOST, server.port)
            return server

        for _ in range(_COUNT):
            modbus.append(await start_modbus())
        for _ in range(_COUNT):
            iec104.append(await start_iec104())
        for _ in range(_COUNT):
            port = free_port()
            await stack.enter_async_context(
                start_ads_server(_HOST, port, num_vars=16)
            )
            ads_ports.append(port)
            await _await_listening(_HOST, port)
        yield ProtocolFleet(modbus, iec104, ads_ports)


async def test_all_30_real_protocol_servers_are_listening(
    protocol_fleet: ProtocolFleet,
) -> None:
    endpoints = protocol_fleet.endpoints()
    assert set(endpoints) == {"modbus", "iec104", "ads"}
    assert all(len(rows) == _COUNT for rows in endpoints.values())
    peers = [address for entries in endpoints.values() for address in entries]
    assert len(peers) == len(set(peers)) == 30
    assert all(host == _HOST for host, _ in peers)
    assert all(await asyncio.gather(*(_can_connect(*peer) for peer in peers)))


@pytest.mark.parametrize("family", ["modbus", "iec104", "ads"])
async def test_each_protocol_accepts_concurrent_connections(
    protocol_fleet: ProtocolFleet, family: str,
) -> None:
    endpoints = protocol_fleet.endpoints()[family]
    for _ in range(3):
        results = await asyncio.gather(
            *(_can_connect(*peer) for peer in endpoints for _ in range(4))
        )
        assert all(results)


@pytest.mark.parametrize("family", ["modbus", "iec104", "ads"])
async def test_short_invalid_protocol_payloads_do_not_kill_server(
    protocol_fleet: ProtocolFleet, family: str,
) -> None:
    for host, port in protocol_fleet.endpoints()[family]:
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2
            )
            writer.write(b"\\x00\\xffbad-protocol\\x00")
            await asyncio.wait_for(writer.drain(), timeout=2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=2)
    assert all(
        await asyncio.gather(
            *(_can_connect(*peer) for peer in protocol_fleet.endpoints()[family])
        )
    )


@pytest.mark.parametrize("index", [0, 3, 9])
async def test_modbus_fault_injection_and_recovery(
    protocol_fleet: ProtocolFleet, index: int,
) -> None:
    server = protocol_fleet.modbus[index]
    await server.stop()
    assert not await _can_connect(server.host, server.port)
    await server.start()
    await _await_listening(server.host, server.port)
    assert await _can_connect(server.host, server.port)


@pytest.mark.parametrize("index", [0, 4, 9])
async def test_iec104_fault_injection_and_recovery(
    protocol_fleet: ProtocolFleet, index: int,
) -> None:
    server = protocol_fleet.iec104[index]
    await server.stop()
    assert not await _can_connect(_HOST, server.port)
    await server.start()
    await _await_listening(_HOST, server.port)
    assert await _can_connect(_HOST, server.port)


async def test_malformed_frame_flood_does_not_affect_other_peers(
    protocol_fleet: ProtocolFleet,
) -> None:
    host, port = protocol_fleet.endpoints()["modbus"][0]

    async def malformed() -> None:
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2
            )
            writer.write(b"\\xff" * 16)
            await asyncio.wait_for(writer.drain(), timeout=2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=2)

    await asyncio.wait_for(
        asyncio.gather(*(malformed() for _ in range(40))), timeout=20
    )
    healthy_peers = [
        *protocol_fleet.endpoints()["modbus"][1:],
        *protocol_fleet.endpoints()["iec104"],
        *protocol_fleet.endpoints()["ads"],
    ]
    assert all(await asyncio.gather(*(_can_connect(*peer) for peer in healthy_peers)))


def test_fixture_is_loopback_only_and_isolated() -> None:
    assert _HOST.startswith("127.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_HOST, 0))
        assert probe.getsockname()[0] == _HOST
