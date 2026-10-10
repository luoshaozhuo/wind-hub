"""CSV 归档段名及重启续号决策；无文件 I/O。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path


def _stem(base: str) -> str:
    if not base.strip():
        raise ValueError("base must not be empty")
    path = Path(base)
    stem = path.stem if path.suffix.lower() == ".csv" else path.name
    if not stem or stem in (".", ".."):
        raise ValueError("invalid file stem")
    return stem


def segment_filename(base: str, *, opened_at: datetime, sequence: int) -> str:
    """UTC 时间戳 + 单调序号，避免轮转及重启后的文件名冲突。"""
    if opened_at.tzinfo is None or opened_at.utcoffset() is None:
        raise ValueError("opened_at must be timezone-aware")
    if sequence < 0:
        raise ValueError("sequence must be nonnegative")
    stamp = opened_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{_stem(base)}.{stamp}.{sequence:06d}.csv"


def is_owned_segment(filename: str, *, base: str) -> bool:
    """仅匹配明确属于当前 Sink 的归档段。"""
    stem = _stem(base)
    return bool(re.fullmatch(
        rf"{re.escape(stem)}\.[0-9]{{8}}T[0-9]{{12}}Z\.[0-9]{{6,}}\.csv",
        filename,
    ))


def next_segment_sequence(filenames: list[str], *, base: str) -> int:
    """重启后从已有归档序号的最大值继续，禁止覆盖旧归档。"""
    latest = -1
    for filename in filenames:
        if is_owned_segment(filename, base=base):
            latest = max(latest, int(filename.rsplit(".", 2)[1]))
    return latest + 1
