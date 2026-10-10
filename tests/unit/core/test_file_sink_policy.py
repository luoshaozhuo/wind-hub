"""CSV 文件容量与轮转边界测试。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.application.csv_sink_record import FileSegment, expired_segments, should_rotate
from core.application.sink_contract import FileSinkConnection


def segment(size: int = 0, *, records: bool = True) -> FileSegment:
    return FileSegment(datetime(2026, 10, 10, 8, tzinfo=UTC), size, records)


def test_default_limits() -> None:
    cfg = FileSinkConnection(path="./data")
    assert cfg.max_size_bytes == 100 * 1024 * 1024
    assert cfg.max_files == 30


def test_exact_size_and_overflow() -> None:
    cfg = FileSinkConnection(path="./data", max_size_mb=1)
    now = datetime(2026, 10, 10, 9, tzinfo=UTC)
    assert not should_rotate(cfg, segment(1024 * 1024 - 10), next_row_bytes=10, now=now)
    assert should_rotate(cfg, segment(1024 * 1024 - 10), next_row_bytes=11, now=now)


def test_no_empty_rotation_for_oversized_row() -> None:
    cfg = FileSinkConnection(path="./data", max_size_mb=1)
    assert not should_rotate(
        cfg, segment(records=False), next_row_bytes=2 * 1024 * 1024,
        now=datetime(2026, 10, 10, 9, tzinfo=UTC),
    )


def test_daily_rotation() -> None:
    cfg = FileSinkConnection(path="./data")
    assert should_rotate(
        cfg, segment(), next_row_bytes=1,
        now=datetime(2026, 10, 11, tzinfo=UTC),
    )


def test_retention_only_selects_old_archives() -> None:
    archived = [Path(f"data.{i}.csv") for i in range(4)]
    assert expired_segments(archived, max_files=2) == tuple(archived[:2])


@pytest.mark.parametrize(
    "options",
    [
        {"max_size_mb": 0},
        {"max_size_mb": float("inf")},
        {"max_files": 0},
        {"buffer_size": 0},
        {"flush_interval": -1},
        {"path": "data.jsonl"},
    ],
)
def test_invalid_config(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FileSinkConnection(path="./data", **options)
