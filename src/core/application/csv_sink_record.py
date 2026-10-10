"""CSV 编码及文件轮转判定；无文件 I/O。"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from core.domain.point_value import PointValue


class FileRotationOptions(Protocol):
    max_size_mb: float


CSV_COLUMNS = ("timestamp", "device_id", "point_id", "value", "quality", "source")


@dataclass(frozen=True, slots=True)
class FileSegment:
    """由 Adapter 维护的当前文件状态。"""

    opened_at: datetime
    byte_size: int
    has_records: bool

    def __post_init__(self) -> None:
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("opened_at must be timezone-aware")
        if self.byte_size < 0:
            raise ValueError("byte_size must be nonnegative")


def should_rotate(
    config: FileRotationOptions,
    segment: FileSegment,
    *,
    next_row_bytes: int,
    now: datetime,
) -> bool:
    """追加下一完整行之前判断轮转。

    单行超过上限时允许该行独占一个超限文件，避免空文件无限轮转。
    Adapter 必须把表头占用计入 byte_size 或 next_row_bytes。
    """
    if next_row_bytes < 0:
        raise ValueError("next_row_bytes must be nonnegative")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not segment.has_records:
        return False
    return (
        now.astimezone(UTC).date() != segment.opened_at.astimezone(UTC).date()
        or segment.byte_size + next_row_bytes > int(config.max_size_mb * 1024 * 1024)
    )


def expired_segments(paths: Sequence[Path], *, max_files: int) -> tuple[Path, ...]:
    """归档文件从旧到新排序；调用方仅传自己拥有的归档文件。"""
    if max_files < 1:
        raise ValueError("max_files must be >= 1")
    return tuple(paths[:max(0, len(paths) - max_files)])


def encode_csv_batch(
    values: Sequence[PointValue], *, include_header: bool = False
) -> bytes:
    """将 PointValue 批次序列化为 UTF-8 CSV。"""
    return b"".join(iter_csv_rows(values, include_header=include_header))


def iter_csv_rows(
    values: Sequence[PointValue], *, include_header: bool = False
) -> Iterator[bytes]:
    """逐条产生完整 CSV 行，便于按行轮转而不拆断记录。"""
    if include_header:
        yield _encode_row(CSV_COLUMNS)
    for item in values:
        yield _encode_row((
            item.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            item.device_id, item.point_id, item.value, item.quality.value, item.source,
        ))


def _encode_row(fields: Sequence[object]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    return buffer.getvalue().encode("utf-8")
