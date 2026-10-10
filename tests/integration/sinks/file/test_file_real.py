"""CSV FileSink 真实文件系统集成测试。"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from collector.domain.point_value import PointValue
from collector.infrastructure.sink.file.csv import FileSink
from core.application.sink_config import FileSinkConnection, ResolvedSinkConfig

pytestmark = pytest.mark.real_service


def sink_for(path: Path, **opts: object) -> FileSink:
    return FileSink(ResolvedSinkConfig(
        name="file", type="file",
        connection=FileSinkConnection(path=str(path), **opts),  # type: ignore[arg-type]
    ))


async def test_csv_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "telemetry.csv"
    sink = sink_for(path)
    await sink.open()
    await sink.write([
        PointValue("WT001", "power", 100.0),
        PointValue("WT001", "status", "online,ok"),
    ])
    await sink.close()
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["value"] == "100.0"
    assert rows[1]["value"] == "online,ok"


async def test_file_limit_and_retention(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.csv"
    unrelated = tmp_path / "notes.csv"
    unrelated.write_text("keep me", encoding="utf-8")
    sink = sink_for(path, max_size_mb=0.0001, max_files=2)
    await sink.open()
    await sink.write([
        PointValue("WT001", f"point-{i}", "long-value-" * 5)
        for i in range(20)
    ])
    await sink.close()
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert len(list(tmp_path.glob("telemetry.*.csv"))) <= 2
    assert path.exists()
