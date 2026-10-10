"""文件轮转与容量契约回归测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.application.file_sink_policy import (
    FileSegment,
    FileSinkPolicy,
    RotationMode,
    RotationReason,
    expired_segments,
    rotation_reason,
)


def segment(*, size: int = 0, records: bool = True) -> FileSegment:
    return FileSegment(
        opened_at=datetime(2026, 10, 10, 8, tzinfo=UTC),
        byte_size=size,
        has_records=records,
    )


def test_defaults_have_bounded_size_and_retention() -> None:
    policy = FileSinkPolicy(path="./data")
    assert policy.max_size_bytes == 100 * 1024 * 1024
    assert policy.max_files == 30
    assert policy.rotation == RotationMode.SIZE_OR_DAILY


def test_size_threshold_exact_fit_is_not_rotated() -> None:
    policy = FileSinkPolicy(path="./data", rotation=RotationMode.SIZE, max_size_mb=1)
    now = datetime(2026, 10, 10, 9, tzinfo=UTC)
    assert rotation_reason(policy, segment(size=1024 * 1024 - 10), pending_bytes=10, now=now) == RotationReason.NONE
    assert rotation_reason(policy, segment(size=1024 * 1024 - 10), pending_bytes=11, now=now) == RotationReason.SIZE


def test_first_oversized_batch_does_not_create_empty_segments() -> None:
    policy = FileSinkPolicy(path="./data", max_size_mb=1)
    now = datetime(2026, 10, 10, 9, tzinfo=UTC)
    assert rotation_reason(policy, segment(records=False), pending_bytes=2 * 1024 * 1024, now=now) == RotationReason.NONE


def test_daily_rotation_uses_utc_date() -> None:
    policy = FileSinkPolicy(path="./data", rotation=RotationMode.DAILY)
    now = datetime(2026, 10, 11, 0, tzinfo=UTC)
    assert rotation_reason(policy, segment(), pending_bytes=1, now=now) == RotationReason.DAILY


def test_size_only_ignores_date_boundary() -> None:
    policy = FileSinkPolicy(path="./data", rotation=RotationMode.SIZE)
    now = datetime(2026, 10, 11, 0, tzinfo=UTC)
    assert rotation_reason(policy, segment(), pending_bytes=1, now=now) == RotationReason.NONE


def test_retention_only_removes_archived_owned_segments() -> None:
    archived = [Path(f"data.{index}.csv") for index in range(4)]
    assert expired_segments(archived, max_files=2) == tuple(archived[:2])


@pytest.mark.parametrize(
    "options",
    [
        {"max_size_mb": 0},
        {"max_files": 0},
        {"buffer_size": 0},
        {"flush_interval": -1},
        {"rotation": "none"},
        {"path": "data.jsonl"},
    ],
)
def test_invalid_file_policy(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FileSinkPolicy(path="./data", **options)
