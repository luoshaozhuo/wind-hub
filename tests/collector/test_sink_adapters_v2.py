"""Redis RESP2 TCP Server 集成测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.redis import RedisSink

pytestmark = pytest.mark.unit


async def _start_resp_stub() -> tuple[asyncio.AbstractServer, list[list[str]]]:
    """假 Redis：PING→PONG，EVAL CAS→:1（总是接受写入），记录全部命令。"""
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
                writer.write(b"+PONG\r\n" if args[0] == "PING" else b":1\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, received


@pytest.mark.asyncio
async def test_redis_pipeline_over_real_tcp() -> None:
    server, received = await _start_resp_stub()
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
        # 时间戳 CAS：EVAL <script> 1 <key> <payload>
        eval_args = received[1]
        assert eval_args[0] == "EVAL"
        assert eval_args[2] == "1"
        assert eval_args[3] == "wind-hub:5:WT001:5:power"
        assert json.loads(eval_args[4])["value"] == 100.0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_redis_drops_non_finite_values_without_failing_batch() -> None:
    server, received = await _start_resp_stub()
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
        await sink.write(
            [
                PointValue("WT001", "power", 100.0),
                PointValue("WT001", "nan_point", float("nan")),
                PointValue("WT001", "inf_point", float("inf")),
            ]
        )
        assert (await sink.health()).healthy
        await sink.close()
        eval_commands = [args for args in received if args[0] == "EVAL"]
        assert len(eval_commands) == 1
        assert json.loads(eval_commands[0][4])["value"] == 100.0
    finally:
        server.close()
        await server.wait_closed()
