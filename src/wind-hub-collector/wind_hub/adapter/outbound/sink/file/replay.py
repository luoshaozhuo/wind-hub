"""File sink 归档回放 —— 读回 CSV / JSONL 并重放到目标 sink。

与采集热路径解耦：这里只提供「解析归档文件 → 逐条 ``PointValue``」与
「按速率把记录写入任意 ``SinkPort``」两段纯逻辑；具体读取哪个文件、重放到
哪个 sink 由 CLI（``wind-hub replay``）负责，CLI 单独创建 sink 实例。

错误处理：单条记录解析失败仅记录日志并跳过；``SinkPort.write`` 失败仅记录
日志并计入失败数，不中断整批重放；结束时返回 :class:`ReplayStats`。
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from wind_hub.application.port.sink import SinkPort
from wind_hub.domain.model.errors import SinkError
from wind_hub.domain.model.point import PointValue, Quality

logger = logging.getLogger(__name__)

_CSV_HEADER = ["device_id", "point_id", "value", "quality", "timestamp", "source"]


@dataclass
class ReplayStats:
    """一次重放的统计结果。"""

    total: int = 0
    """尝试写入 sink 的记录总数（不含解析失败被跳过的行）。"""

    success: int = 0
    """写入成功的记录数。"""

    failed: int = 0
    """写入失败的记录数。"""

    elapsed_seconds: float = 0.0
    """重放耗时（秒）。"""


def parse_records(path: Path, fmt: str) -> list[PointValue]:
    """读取归档文件并解析为 ``PointValue`` 列表。

    Args:
        path: 归档文件路径（``csv`` 或 ``jsonl``）。
        fmt: 格式（``"csv"`` 或 ``"jsonl"``）。

    单行解析失败（字段缺失、JSON 非法、时间戳非法、值无法归因等）只记录日志
    并跳过，不影响其它行。
    """
    if fmt == "jsonl":
        return _parse_jsonl(path)
    return _parse_csv(path)


def _parse_jsonl(path: Path) -> list[PointValue]:
    records: list[PointValue] = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(
                    PointValue(
                        device_id=data["device_id"],
                        point_id=data["point_id"],
                        value=data["value"],
                        quality=Quality(data.get("quality", Quality.GOOD.value)),
                        timestamp=datetime.fromisoformat(data["timestamp"]),
                        source=data.get("source"),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("skip invalid jsonl line %d in %s: %s", line_number, path, exc)
    return records


def _parse_csv(path: Path) -> list[PointValue]:
    records: list[PointValue] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for line_number, row in enumerate(reader, start=2):
            try:
                records.append(
                    PointValue(
                        device_id=row["device_id"],
                        point_id=row["point_id"],
                        value=_coerce_value(row["value"]),
                        quality=Quality(row["quality"]),
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        source=row["source"] or None,
                    )
                )
            except (KeyError, ValueError) as exc:
                logger.warning("skip invalid csv line %d in %s: %s", line_number, path, exc)
    return records


def _coerce_value(raw: str) -> Any:
    """把 CSV 单元格字符串还原为原始值类型（空串 → ``None``，其余 int/float/bool/str）。"""
    if raw == "":
        return None
    if raw == "True":
        return True
    if raw == "False":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


async def replay_to_sink(
    sink: SinkPort,
    records: Iterable[PointValue],
    rate: int | None,
) -> ReplayStats:
    """把记录逐条写入 ``sink``，按 ``rate`` 限速，返回统计结果。

    Args:
        sink: 目标 sink（已 ``open``）。
        records: 待重放的记录。
        rate: 每秒重放条数；``None`` 或 ``<= 0`` 时不限速。

    单条 ``write`` 失败仅记录日志并计入 ``failed``，不中断其余记录。
    """
    stats = ReplayStats()
    started = time.monotonic()
    interval = 1.0 / rate if rate is not None and rate > 0 else 0.0

    for pv in records:
        stats.total += 1
        try:
            await sink.write([pv])
            stats.success += 1
        except SinkError as exc:
            logger.warning("replay write failed for %s/%s: %s", pv.device_id, pv.point_id, exc)
            stats.failed += 1
        if interval > 0:
            await asyncio.sleep(interval)

    stats.elapsed_seconds = time.monotonic() - started
    return stats
