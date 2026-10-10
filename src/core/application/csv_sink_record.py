"""FileSink CSV 行编码，标准库实现、无磁盘 I/O。"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import UTC

from core.application.file_sink_policy import CSV_COLUMNS
from core.domain.point_value import PointValue


def encode_csv_batch(
    values: Sequence[PointValue],
    *,
    include_header: bool = False,
) -> bytes:
    """编码一个完整批次；输出 UTF-8、RFC 4180 风格的 CSV 行。

    None 表示缺失值，输出空字段；字符串正确转义逗号、双引号、换行。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    if include_header:
        writer.writerow(CSV_COLUMNS)
    for item in values:
        writer.writerow(
            (
                item.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                item.device_id,
                item.point_id,
                item.value,
                item.quality.value,
                item.source,
            )
        )
    return buffer.getvalue().encode("utf-8")
