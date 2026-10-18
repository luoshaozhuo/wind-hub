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


async def test_write_refuses_segment_symlink_pointing_outside(tmp_path: Path) -> None:
    """分段文件被预置为指向根外的符号链接时，O_NOFOLLOW 必须拒绝写入。"""
    root = tmp_path / "archive"
    outside = tmp_path / "outside.csv"
    outside.write_text("ORIGINAL", encoding="utf-8")
    sink = make_sink(root)
    await sink.open()
    day = datetime.now(UTC).strftime("%Y%m%d")
    directory = root / "t1" / "d" / day[:4] / day[4:6]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{day}_000.csv").symlink_to(outside)

    with pytest.raises(SinkError, match="symlink"):
        await sink.write_task("t1", [_point("p")])
    assert outside.read_text(encoding="utf-8") == "ORIGINAL"
    await sink.close()


async def test_write_refuses_symlinked_archive_directory(tmp_path: Path) -> None:
    """归档中间目录被替换为指向根外的符号链接时，归属校验拒绝写入。"""
    root = tmp_path / "archive"
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    sink = make_sink(root)
    await sink.open()
    now = datetime.now(UTC)
    day = now.strftime("%Y%m%d")
    # 将真实写入路径上的月目录替换为指向根外的链接。
    year_dir = root / "t1" / "d" / day[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    (year_dir / day[4:6]).symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(SinkError, match="escapes root"):
        await sink.write_task("t1", [_point("p", when=now)])
    assert list(outside_dir.iterdir()) == []
    await sink.close()


async def test_cleanup_never_unlinks_symlinked_segments(tmp_path: Path) -> None:
    """过期分段若为符号链接（哪怕指向根内），cleanup 只跳过、不 unlink 目标。"""
    root = tmp_path / "archive"
    target = tmp_path / "real_target.csv"
    target.write_text("KEEP", encoding="utf-8")
    sink = make_sink(root, retention_days=1)
    old_day = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y%m%d")
    link = _segment(root, "t1", "d", old_day, 0)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    await sink.open()  # open 触发 force cleanup
    await sink.close()
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "KEEP"


async def test_batch_opens_each_segment_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "archive"
    sink = make_sink(root)
    opened: list[str] = []
    original = sink._open_segment

    def tracked(path: str, flags: int, *, dir_fd: int) -> int:
        opened.append(path)
        return original(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(sink, "_open_segment", tracked)
    await sink.open()
    await sink.write_task("t1", [_point(f"p{i}", i) for i in range(500)])
    await sink.close()
    assert len(opened) == 1, "one batch in one segment must use one file open"
    day = datetime.now(UTC).strftime("%Y%m%d")
    with _segment(root, "t1", "d", day, 0).open(newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 500


async def test_oversized_row_does_not_loop_or_lose_following_rows(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    sink = make_sink(root, max_size_mb=0.0001)
    await sink.open()
    await sink.write_task("t1", [_point("huge", "z" * 2000), _point("small", 1)])
    await sink.close()
    day = datetime.now(UTC).strftime("%Y%m%d")
    segments = sorted((root / "t1" / "d" / day[:4] / day[4:6]).glob("*.csv"))
    rows = []
    for segment in segments:
        with segment.open(newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    assert [r["point_id"] for r in rows] == ["huge", "small"]
    assert len(segments) == 2


async def test_directory_symlink_inside_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    sink = make_sink(root)
    await sink.open()
    now = datetime.now(UTC)
    actual = root / "safe"
    actual.mkdir()
    (root / "t1").symlink_to(actual, target_is_directory=True)
    with pytest.raises(SinkError, match="symlink"):
        await sink.write_task("t1", [_point("power", when=now)])
    assert not list(actual.rglob("*.csv"))
    await sink.close()


async def test_batch_appends_after_reopen_without_extra_header(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    sink = make_sink(root)
    await sink.open()
    await sink.write_task("t1", [_point(f"a{i}", i) for i in range(250)])
    await sink.write_task("t1", [_point(f"b{i}", i) for i in range(250)])
    await sink.close()
    day = datetime.now(UTC).strftime("%Y%m%d")
    with _segment(root, "t1", "d", day, 0).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 500
    assert rows[0]["point_id"] == "a0"
    assert rows[-1]["point_id"] == "b249"
