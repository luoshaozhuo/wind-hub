"""CSV 文件写入和 Redis RESP2 TCP Server 集成测试。"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

from collector.infrastructure.sink.file.csv import FileSink
from collector.infrastructure.sink.redis import RedisSink
from core.application.sink_config import (
    FileSinkConnection,
    RedisSinkConnection,
    ResolvedSinkConfig,
)
from core.domain.point_value import PointValue


@pytest.mark.asyncio
async def test_csv_appends_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    cfg = ResolvedSinkConfig(
        name="archive", type="file",
        connection=FileSinkConnection(path=str(path)),
    )
    sink = FileSink(cfg)
    await sink.open()
    await sink.write([PointValue("WT001", "power", 125.5)])
    await sink.close()
    await sink.open()
    await sink.write([PointValue("WT001", "speed", 8.4)])
    await sink.close()
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["point_id"] for row in rows] == ["power", "speed"]


@pytest.mark.asyncio
async def test_csv_rotates_and_prunes_owned_archives(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    cfg = ResolvedSinkConfig(
        name="archive", type="file",
        connection=FileSinkConnection(path=str(path), max_size_mb=0.0001, max_files=2),
    )
    sink = FileSink(cfg)
    await sink.open()
    await sink.write([PointValue("WT001", f"point{i}", "x" * 60) for i in range(12)])
    await sink.close()
    assert len(list(tmp_path.glob("data.*.csv"))) <= 2
    assert path.exists()


@pytest.mark.asyncio
async def test_redis_pipeline_over_real_tcp() -> None:
    received: list[list[str]] = []

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
        cfg = ResolvedSinkConfig(
            name="cache", type="redis",
            connection=RedisSinkConnection(host="127.0.0.1", port=port),
        )
        sink = RedisSink(cfg)
        await sink.open()
        await sink.write([PointValue("WT001", "power", 100.0)])
        assert sink.health().healthy
        await sink.close()
        assert not sink.health().healthy
        assert received[0] == ["PING"]
        assert received[1][0] == "SET"
        assert received[1][1] == "wind-hub:5:WT001:5:power"
        assert json.loads(received[1][2])["value"] == 100.0
    finally:
        server.close()
        await server.wait_closed()
