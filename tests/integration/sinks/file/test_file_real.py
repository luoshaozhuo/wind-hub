"""CSV FileSink 真实文件系统集成测试。

单元测试（tests/unit/collector/sink/test_file_sink.py）已覆盖路径安全与
轮转边界；这里在真实文件系统上验证端到端可观测契约：
``{root}/{task}/{device}/YYYY/MM/YYYYMMDD_NNN.csv`` 分段可被标准 csv
模块无损解析，容量轮转不丢行，期限清理只动本 Sink 拥有的过期分段。
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.file import FileSink

pytestmark = pytest.mark.real_service


def sink_for(root: Path, **opts: object) -> FileSink:
    return FileSink(
        Sink(
            sink_id="file",
            kind="file",
            enabled=True,
            connection={"path": str(root), **opts},
        )
    )


def _segments(root: Path, task_id: str, device_id: str) -> list[Path]:
    return sorted(root.glob(f"{task_id}/{device_id}/????/??/????????_???.csv"))


async def test_csv_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "archive"
    sink = sink_for(root)
    await sink.open()
    await sink.write_task(
        "task-a",
        [
            PointValue("WT001", "power", 100.0),
            PointValue("WT001", "status", "online,ok"),
        ],
    )
    await sink.close()

    segments = _segments(root, "task-a", "WT001")
    assert len(segments) == 1
    with segments[0].open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["value"] == "100.0"
    assert rows[1]["value"] == "online,ok"


async def test_size_rotation_preserves_all_rows(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    sink = sink_for(root, max_size_mb=0.0001)
    await sink.open()
    await sink.write_task(
        "task-a",
        [PointValue("WT001", f"point-{i:02d}", "long-value-" * 5) for i in range(20)],
    )
    await sink.close()

    segments = _segments(root, "task-a", "WT001")
    assert len(segments) > 1, "tiny size limit must produce multiple segments"
    rows: list[dict[str, str]] = []
    for segment in segments:
        with segment.open(newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    assert len(rows) == 20
    assert {row["point_id"] for row in rows} == {f"point-{i:02d}" for i in range(20)}


async def test_retention_removes_only_expired_owned_segments(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    # 归档布局外的无关文件必须原样保留。
    unrelated = tmp_path / "notes.csv"
    unrelated.write_text("keep me", encoding="utf-8")

    # 手工放置一个 40 天前的过期分段（目录/文件名遵循归档约定）。
    old_day = (datetime.now(UTC).date() - timedelta(days=40)).strftime("%Y%m%d")
    expired = root / "task-a" / "WT001" / old_day[:4] / old_day[4:6] / f"{old_day}_000.csv"
    expired.parent.mkdir(parents=True)
    expired.write_text("device_id,point_id,value,timestamp\nold,old,1,2000\n", encoding="utf-8")

    sink = sink_for(root, retention_days=7)
    await sink.open()  # open() 强制执行一次期限清理
    await sink.write_task("task-a", [PointValue("WT001", "power", 1.0)])
    await sink.close()

    assert not expired.exists(), "segment older than retention window must be removed"
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    today_segments = _segments(root, "task-a", "WT001")
    assert today_segments, "current-day segment must survive cleanup"
