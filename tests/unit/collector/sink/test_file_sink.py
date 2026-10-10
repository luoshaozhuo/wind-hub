"""CSV FileSink 单元测试：追加、轮转与生命周期。"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from collector.application.errors import SinkError
from collector.domain.point_value import PointValue
from collector.infrastructure.sink.file.csv import FileSink
from core.application.sink_config import FileSinkConnection, ResolvedSinkConfig


def make_sink(path: Path, *, max_size_mb: float = 100, max_files: int = 30) -> FileSink:
    return FileSink(
        ResolvedSinkConfig(
            name="file",
            type="file",
            connection=FileSinkConnection(
                path=str(path), max_size_mb=max_size_mb, max_files=max_files
            ),
        )
    )


async def test_csv_write_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "points.csv"
    sink = make_sink(path)
    await sink.open()
    await sink.open()
    await sink.write([PointValue("d", "power", 12.0)])
    await sink.flush()
    await sink.close()
    await sink.close()
    await sink.open()
    await sink.write([PointValue("d", "wind", 8.0)])
    await sink.close()
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [row["point_id"] for row in rows] == ["power", "wind"]


async def test_csv_before_open_fails(tmp_path: Path) -> None:
    sink = make_sink(tmp_path / "points.csv")
    with pytest.raises(SinkError, match="opened"):
        await sink.write([PointValue("d", "p", 1.0)])


async def test_csv_rotates_and_limits_history(tmp_path: Path) -> None:
    sink = make_sink(tmp_path / "points.csv", max_size_mb=0.0001, max_files=2)
    await sink.open()
    await sink.write([PointValue("d", f"p{i}", "x" * 64) for i in range(20)])
    await sink.close()
    assert len(list(tmp_path.glob("points.*.csv"))) <= 2
    assert (tmp_path / "points.csv").exists()


async def test_csv_empty_batch_no_new_record(tmp_path: Path) -> None:
    path = tmp_path / "points.csv"
    sink = make_sink(path)
    await sink.open()
    await sink.write([])
    await sink.close()
    with path.open(newline="", encoding="utf-8") as f:
        assert len(list(csv.reader(f))) == 1
