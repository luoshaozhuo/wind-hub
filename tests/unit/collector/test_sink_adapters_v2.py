"""CSV 文件写入和 Redis RESP2 TCP Server 集成测试。"""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.file import FileSink
from core.infrastructure.sink.redis import RedisSink


@pytest.mark.asyncio
async def test_csv_appends_and_reopens(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    cfg = Sink(sink_id="archive", kind="file", enabled=True, connection={"path": str(root)})
    sink = FileSink(cfg)
    await sink.open()
    await sink.write_task("t1", [PointValue("WT001", "power", 125.5)])
    await sink.close()
    await sink.open()
    await sink.write_task("t1", [PointValue("WT001", "speed", 8.4)])
    await sink.close()
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = root / "t1" / "WT001" / day[:4] / day[4:6] / f"{day}_000.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["point_id"] for row in rows] == ["power", "speed"]


@pytest.mark.asyncio
async def test_csv_rotates_by_size(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    cfg = Sink(
        sink_id="archive",
        kind="file",
        enabled=True,
        connection={"path": str(root), "max_size_mb": 0.0001},
    )
    sink = FileSink(cfg)
    await sink.open()
    await sink.write_task("t1", [PointValue("WT001", f"point{i}", "x" * 60) for i in range(12)])
    await sink.close()
    day = datetime.now(UTC).strftime("%Y%m%d")
    directory = root / "t1" / "WT001" / day[:4] / day[4:6]
    segments = sorted(directory.glob(f"{day}_*.csv"))
    assert len(segments) >= 2
    assert all(path.stat().st_size > 0 for path in segments)


@pytest.mark.asyncio
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
