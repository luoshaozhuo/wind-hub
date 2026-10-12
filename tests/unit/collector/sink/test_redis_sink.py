"""RedisSink RESP2 协议测试：真实 TCP 回环 server 验证管线编码。"""

from __future__ import annotations

import asyncio
import json

from collector.infrastructure.sink.redis import RedisSink
from core.domain import Sink
from core.domain.point_value import PointValue


async def test_redis_pipeline_over_real_tcp() -> None:
    received: list[list[str]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readline()
                if not header:
                    break
                count = int(header[1:])
                args: list[str] = []
                for _ in range(count):
                    size = int((await reader.readline())[1:])
                    data = await reader.readexactly(size)
                    await reader.readexactly(2)
                    args.append(data.decode("utf-8"))
                received.append(args)
                writer.write(b"+PONG\r\n" if args[0] == "PING" else b"+OK\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        cfg = Sink(
            sink_id="cache",
            kind="redis",
            enabled=True,
            connection={"host": "127.0.0.1", "port": port},
        )
        sink = RedisSink(cfg)
        await sink.open()
        await sink.write([PointValue("WT001", "power", 100.0)])
        assert (await sink.health()).healthy
        await sink.close()
        assert not (await sink.health()).healthy
        assert received[0] == ["PING"]
        assert received[1][0] == "SET"
        assert received[1][1] == "wind-hub:5:WT001:5:power"
        assert json.loads(received[1][2])["value"] == 100.0
    finally:
        server.close()
        await server.wait_closed()
