"""CSV / Redis Sink 实际接口测试（不依赖 Redis 服务）。"""

from __future__ import annotations

import csv
import io
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
    assert len(rows) == 2
    assert rows[0]["point_id"] == "power"
    assert rows[1]["point_id"] == "speed"


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
    archived = list(tmp_path.glob("data.*.csv"))
    assert len(archived) <= 2
    assert path.exists()


class Pipeline:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.executed = False

    async def __aenter__(self) -> Pipeline:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def set(self, key: str, payload: str) -> None:
        self.commands.append((key, payload))

    async def execute(self) -> None:
        self.executed = True


class FakeRedis:
    def __init__(self) -> None:
        self.pipe = Pipeline()
        self.closed = False

    def pipeline(self, *, transaction: bool) -> Pipeline:
        assert transaction is False
        return self.pipe

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_redis_batch_pipeline_and_close() -> None:
    cfg = ResolvedSinkConfig(
        name="cache", type="redis",
        connection=RedisSinkConnection(host="127.0.0.1"),
    )
    sink = RedisSink(cfg)
    fake = FakeRedis()
    sink._client = fake
    await sink.write([PointValue("WT001", "power", 100.0)])
    assert fake.pipe.executed
    key, payload = fake.pipe.commands[0]
    assert key == "wind-hub:5:WT001:5:power"
    assert json.loads(payload)["quality"] == "good"
    await sink.close()
    assert fake.closed
