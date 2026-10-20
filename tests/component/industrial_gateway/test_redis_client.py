"""Redis RESP2 MGET 客户端组件测试（无需外部 Redis）。"""

from __future__ import annotations

import asyncio

from industrial_gateway.redis_client import RedisBatchReader


async def test_mget_reads_bulk_values_and_reuses_connection() -> None:
    connections = 0

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        try:
            for _ in range(2):
                header = await reader.readline()
                if not header:
                    return
                argc = int(header[1:])
                parts = []
                for _ in range(argc):
                    size_line = await reader.readline()
                    size = int(size_line[1:])
                    parts.append((await reader.readexactly(size + 2))[:-2].decode())
                assert parts == ["MGET", "point:1", "point:2"]
                writer.write(b"*2\r\n$3\r\none\r\n$-1\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = RedisBatchReader("127.0.0.1", port)
    try:
        assert await client.mget(["point:1", "point:2"]) == [b"one", None]
        assert await client.mget(["point:1", "point:2"]) == [b"one", None]
        assert connections == 1
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
