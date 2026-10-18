"""CSV 编码；无文件 I/O。"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

from core.domain.point_value import PointValue

CSV_COLUMNS = (
    "timestamp",
    "device_id",
    "point_id",
    "value",
    "quality",
    "source",
    "receive_time",
    "device_time",
)


def encode_csv_batch(values: Sequence[PointValue], *, include_header: bool = False) -> bytes:
    """将 PointValue 批次序列化为 UTF-8 CSV。"""
    return b"".join(iter_csv_rows(values, include_header=include_header))


def _utc_z(value: datetime | None) -> str:
    """带时区时间格式化为 ``...Z``；``None`` 落为空列。"""
    if value is None:
        return ""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def iter_csv_rows(values: Sequence[PointValue], *, include_header: bool = False) -> Iterator[bytes]:
    """逐条产生完整 CSV 行，便于按行轮转而不拆断记录。"""
    if include_header:
        yield _encode_row(CSV_COLUMNS)
    for item in values:
        yield _encode_row(
            (
                _utc_z(item.timestamp),
                item.device_id,
                item.point_id,
                item.value,
                item.quality.value,
                item.source,
                _utc_z(item.receive_time),
                _utc_z(item.device_time),
            )
        )


def _encode_row(fields: Sequence[object]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    return buffer.getvalue().encode("utf-8")
