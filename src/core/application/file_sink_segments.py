"""CSV 文件段命名和重启恢复所需的纯文件名规则。

不执行文件扫描、删除或文件句柄操作；由后续 File Adapter 管理。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path


def segment_filename(base: str, *, opened_at: datetime, sequence: int) -> str:
    """生成可按字典序排序的 UTC 段名；序号避免同一秒多次轮转冲突。"""
    if not base.strip():
        raise ValueError("base must not be empty")
    if opened_at.tzinfo is None or opened_at.utcoffset() is None:
        raise ValueError("opened_at must be timezone-aware")
    if sequence < 0:
        raise ValueError("sequence must be nonnegative")
    stem = Path(base).stem if Path(base).suffix.lower() == ".csv" else Path(base).name
    if not stem:
        raise ValueError("file stem must not be empty")
    instant = opened_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stem}.{instant}.{sequence:06d}.csv"


def is_owned_segment(filename: str, *, base: str) -> bool:
    """仅识别当前 Sink 的时间戳归档文件，防止清理无关文件。"""
    stem = Path(base).stem if Path(base).suffix.lower() == ".csv" else Path(base).name
    if not stem:
        return False
    pattern = re.compile(
        rf"^{re.escape(stem)}\.[0-9]{{8}}T[0-9]{{12}}Z\.[0-9]{{6,}}\.csv$"
    )
    return bool(pattern.fullmatch(filename))
