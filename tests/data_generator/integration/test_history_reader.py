"""UC-02 集成：真实 CSV→SQLite Reader（iter_records）与异常记录处理。

文件均落真实临时目录，SQLite 索引/排序/取消为生产代码路径；另以仓库真实
configs/history/demo.csv 与 resolve_history 做基线核对。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from data_generator.application.replay import iter_records
from data_generator.domain.model import GeneratorError
from data_generator.domain.scenario import resolve_history

from ..conftest import REPO_CONFIGS

pytestmark = [pytest.mark.integration, pytest.mark.reliability, pytest.mark.invalid_records]

T0 = datetime(2026, 1, 1, tzinfo=UTC)
POINT_TYPES = {("GW02", "p"): "float64"}


def _write(tmp_path: Path, name: str, lines: list[str], *, raw: bool = False) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw:
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    header = "device_id,point_id,source_timestamp,value,quality,record_id"
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return path


async def _collect(source: Path, start=T0, end=None, **kwargs: Any) -> list[Any]:
    records: list[Any] = []
    async for record in iter_records(
        source,
        start,
        end or T0 + timedelta(hours=1),
        point_types=kwargs.pop("point_types", POINT_TYPES),
        **kwargs,
    ):
        records.append(record)
    return records


async def test_shipped_demo_csv_reads_three_records_in_order() -> None:
    source = resolve_history(REPO_CONFIGS, "history/demo.csv")
    types = {("GW02", "a02_dm_pitchangle1"): "float32"}
    records = await _collect(
        source,
        point_types=types,
    )
    assert [record.record_id for record in records] == ["0001", "0002", "0003"]
    assert [float(record.value) for record in records] == [5.5, 6.0, 7.5]


async def test_resolve_history_confines_to_history_directory(tmp_path: Path) -> None:
    with pytest.raises(GeneratorError):
        resolve_history(tmp_path, "../escape.csv")
    with pytest.raises(GeneratorError):
        resolve_history(tmp_path, "/etc/passwd")


async def test_header_must_match_exactly(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "bad.csv",
        ["device_id,point_id,source_timestamp,value,quality", "GW02,p,2026-01-01T00:00:00Z,1,GOOD"],
        raw=True,
    )
    with pytest.raises(GeneratorError, match="header"):
        await _collect(source)


async def test_extra_data_columns_rejected(tmp_path: Path) -> None:
    # 表头仍为 6 列，但数据行多出一列 → DictReader restkey=None → 逐行拒绝。
    source = _write(
        tmp_path,
        "extra.csv",
        ["GW02,p,2026-01-01T00:00:00Z,1,GOOD,r1,surplus"],
    )
    with pytest.raises(GeneratorError, match="invalid CSV line 2"):
        await _collect(source)


async def test_extra_header_column_rejected(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "extra-header.csv",
        [
            "device_id,point_id,source_timestamp,value,quality,record_id,extra",
            "GW02,p,2026-01-01T00:00:00Z,1,GOOD,r1,x",
        ],
        raw=True,
    )
    with pytest.raises(GeneratorError, match="header"):
        await _collect(source)


async def test_non_utc_timestamp_rejected(tmp_path: Path) -> None:
    source = _write(tmp_path, "tz.csv", ["GW02,p,2026-01-01T02:00:00+02:00,1,GOOD,r1"])
    with pytest.raises(GeneratorError, match="UTC"):
        await _collect(source)
    with pytest.raises(GeneratorError):
        bad = _write(tmp_path, "naive.csv", ["GW02,p,not-a-date,1,GOOD,r1"])
        await _collect(bad)


async def test_unmapped_point_and_bad_value_rejected(tmp_path: Path) -> None:
    source = _write(tmp_path, "p.csv", ["GW99,p,2026-01-01T00:00:00Z,1,GOOD,r1"])
    with pytest.raises(GeneratorError, match="unmapped replay source point"):
        await _collect(source)
    bad_value = _write(tmp_path, "v.csv", ["GW02,p,2026-01-01T00:00:00Z,not-number,GOOD,r1"])
    with pytest.raises(GeneratorError, match="invalid float64 value"):
        await _collect(bad_value)


async def test_empty_field_and_duplicate_record_id_rejected(tmp_path: Path) -> None:
    empty = _write(tmp_path, "e.csv", ["GW02,p,2026-01-01T00:00:00Z,,GOOD,r1"])
    with pytest.raises(GeneratorError, match="invalid CSV line 2"):
        await _collect(empty)
    dup = _write(
        tmp_path,
        "d.csv",
        [
            "GW02,p,2026-01-01T00:00:00Z,1,GOOD,same",
            "GW02,p,2026-01-01T00:00:01Z,2,GOOD,same",
        ],
    )
    with pytest.raises(GeneratorError, match="invalid CSV line 3"):
        await _collect(dup)


async def test_byte_limit_enforced_before_index(tmp_path: Path) -> None:
    source = _write(tmp_path, "big.csv", ["GW02,p,2026-01-01T00:00:00Z,1,GOOD,r1"])
    with pytest.raises(GeneratorError, match="byte limit"):
        await _collect(source, max_file_bytes=10)


async def test_temporary_index_does_not_survive_completion(tmp_path: Path) -> None:
    source = _write(tmp_path, "ok.csv", ["GW02,p,2026-01-01T00:00:00Z,1,GOOD,r1"])
    records = await _collect(source)
    assert len(records) == 1
    leftovers = list(Path("/tmp").glob("windhub-replay-*"))
    # 生产实现用 TemporaryDirectory；正常结束后不应留下本次索引目录。
    assert all(not item.is_dir() for item in leftovers if (item / "replay.sqlite3").exists())


async def test_cancellation_waits_for_index_thread(tmp_path: Path) -> None:
    # ~40k 行让索引线程存活足够久，取消迭代后事件循环不得被遗留线程破坏。
    rows = [
        f"GW02,p,2026-01-01T00:00:{i // 1000:02d}.{i % 1000:03d}Z,1,GOOD,rid-{i}"
        for i in range(40_000)
    ]
    source = _write(tmp_path, "large.csv", rows)

    async def consume() -> None:
        async for _record in iter_records(
            source, T0, T0 + timedelta(hours=1), point_types=POINT_TYPES
        ):
            return

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 事件循环仍可正常工作，且可以再次完整读取同一文件。
    await asyncio.sleep(0.05)
    records = await _collect(source, read_batch_size=5000)
    assert len(records) == 40_000
