"""CSV FileSink 单元测试：Task/Device 归档、追加、容量轮转与保留清理。"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.application.sink_error import SinkError
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.file import FileSink


def make_sink(
    root: Path, *, max_size_mb: float = 100, retention_days: int = 30
) -> FileSink:
    return FileSink(
        Sink(
            sink_id="file",
            kind="file",
            enabled=True,
            connection={
                "path": str(root),
                "max_size_mb": max_size_mb,
                "retention_days": retention_days,
            },
        )
    )


def _point(
    point_id: str, value: object = 1.0, *, when: datetime | None = None
) -> PointValue:
    return PointValue("d", point_id, value, timestamp=when or datetime.now(UTC))


def _segment(root: Path, task: str, device: str, day: str, index: int) -> Path:
    return root / task / device / day[:4] / day[4:6] / f"{day}_{index:03d}.csv"


async def test_csv_write_and_reopen(tmp_path: Path) -> None:
    sink = make_sink(tmp_path / "archive")
    await sink.open()
    await sink.open()  # 重复 open 幂等
    await sink.write_task("t1", [_point("power", 12.0)])
    await sink.flush()
    await sink.close()
    await sink.close()  # 重复 close 幂等
    await sink.open()
    await sink.write_task("t1", [_point("wind", 8.0)])
    await sink.close()
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = _segment(tmp_path / "archive", "t1", "d", day, 0)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [row["point_id"] for row in rows] == ["power", "wind"]


async def test_csv_before_open_fails(tmp_path: Path) -> None:
    sink = make_sink(tmp_path / "archive")
    with pytest.raises(SinkError, match="opened"):
        await sink.write_task("t1", [_point("p")])
    # 无 Task 身份的裸写入必须被拒绝，不能落盘到任何目录。
    with pytest.raises(SinkError, match="task_id"):
        await sink.write([_point("p")])
    assert not list(tmp_path.rglob("*.csv"))


async def test_csv_rotates_by_size_without_overwriting_history(tmp_path: Path) -> None:
    # 约 105 字节上限：header 后每写一两条就必须切换新分段。
    sink = make_sink(tmp_path / "archive", max_size_mb=0.0001)
    await sink.open()
    await sink.write_task("t1", [_point(f"p{i:02d}", "x" * 64) for i in range(20)])
    await sink.close()
    day = datetime.now(UTC).strftime("%Y%m%d")
    directory = tmp_path / "archive" / "t1" / "d" / day[:4] / day[4:6]
    segments = sorted(directory.glob(f"{day}_*.csv"))
    assert len(segments) >= 2
    # 历史分段不得被覆盖：每个分段都带 header 且非空，全部数据行总数为 20。
    total = 0
    for path in segments:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows, f"{path.name} 为空文件，历史分段被覆盖"
        total += len(rows)
    assert total == 20


async def test_csv_empty_batch_creates_no_file(tmp_path: Path) -> None:
    sink = make_sink(tmp_path / "archive")
    await sink.open()
    await sink.write_task("t1", [])
    await sink.close()
    assert not list(tmp_path.rglob("*.csv"))


async def test_late_data_uses_its_sample_day_directory(tmp_path: Path) -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    sink = make_sink(tmp_path / "archive")
    await sink.open()
    await sink.write_task("t1", [_point("late", 1.0, when=yesterday)])
    await sink.close()
    day = yesterday.strftime("%Y%m%d")
    assert _segment(tmp_path / "archive", "t1", "d", day, 0).exists()


async def test_cleanup_removes_only_expired_owned_segments(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    sink = make_sink(root, retention_days=7)
    # 过期自有分段（10 天前）应在 open 清理中删除。
    old_day = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y%m%d")
    old_file = _segment(root, "t1", "d", old_day, 0)
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("stale\n", encoding="utf-8")
    # 不符合 task/device/年/月 归属结构的文件不得被清理触碰。
    foreign = root / "other.csv"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("foreign\n", encoding="utf-8")

    await sink.open()
    await sink.close()
    assert not old_file.exists()
    assert foreign.exists()


async def test_unsafe_task_or_device_component_rejected(tmp_path: Path) -> None:
    sink = make_sink(tmp_path / "archive")
    await sink.open()
    with pytest.raises(SinkError, match="unsafe archive path component"):
        await sink.write_task("../escape", [_point("p")])
    evil = PointValue("../dev", "p", 1.0)
    with pytest.raises(SinkError, match="unsafe archive path component"):
        await sink.write_task("t1", [evil])
