"""UR-02：重放调度倍率、稳定排序与时间窗口（真实 CSV+SQLite，设计 §10）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from data_generator.application.replay import (
    iter_records,
    replay_deadline,
)
from data_generator.domain.model import GeneratorError

pytestmark = [pytest.mark.unit, pytest.mark.reliability, pytest.mark.time_order]

T0 = datetime(2026, 1, 1, tzinfo=UTC)

POINT_TYPES = {("GW02", "p"): "float64"}


def _csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "history.csv"
    lines = ["device_id,point_id,source_timestamp,value,quality,record_id", *rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _records(source: Path, start: datetime, end: datetime, **kwargs: Any) -> list[Any]:
    out: list[Any] = []
    async for record in iter_records(source, start, end, point_types=POINT_TYPES, **kwargs):
        out.append(record)
    return out


def test_deadline_scales_elapsed_source_time_by_speed() -> None:
    # speed=2：2 秒源时间在墙钟上仅推进 1 秒。
    assert replay_deadline(T0, T0 + timedelta(seconds=2), 100.0, 2.0) == pytest.approx(101.0)
    assert replay_deadline(
        T0 + timedelta(seconds=10), T0 + timedelta(seconds=13), 0.0, 1.0
    ) == pytest.approx(3.0)


def test_deadline_rejects_non_positive_speed() -> None:
    with pytest.raises(GeneratorError):
        replay_deadline(T0, T0 + timedelta(seconds=1), 0.0, 0.0)


async def test_equal_timestamps_ordered_by_source_line(tmp_path: Path) -> None:
    rows = [f"GW02,p,2026-01-01T00:00:00Z,{i + 0.5},GOOD,{1000 + i}" for i in range(5)]
    source = _csv(tmp_path, rows)
    records = await _records(source, T0, T0 + timedelta(hours=1))
    assert [record.line_number for record in records] == [2, 3, 4, 5, 6]
    assert [float(record.value) for record in records] == [0.5, 1.5, 2.5, 3.5, 4.5]


async def test_window_is_half_open_and_ordered(tmp_path: Path) -> None:
    rows = [
        "GW02,p,2026-01-01T00:00:00Z,1,GOOD,r1",
        "GW02,p,2026-01-01T00:00:01Z,2,GOOD,r2",
        "GW02,p,2026-01-01T00:00:02Z,3,GOOD,r3",
        "GW02,p,2026-01-01T00:00:03Z,4,GOOD,r4",
    ]
    source = _csv(tmp_path, rows)
    records = await _records(
        source,
        T0 + timedelta(seconds=1),
        T0 + timedelta(seconds=3),
    )
    assert [record.record_id for record in records] == ["r2", "r3"]


async def test_pagination_keeps_total_order(tmp_path: Path) -> None:
    rows = [
        f"GW02,p,2026-01-01T00:00:{second:02d}Z,{second},GOOD,id-{second}" for second in range(5)
    ]
    source = _csv(tmp_path, rows)
    records = await _records(source, T0, T0 + timedelta(hours=1), read_batch_size=2)
    assert [record.record_id for record in records] == [
        "id-0",
        "id-1",
        "id-2",
        "id-3",
        "id-4",
    ]


async def test_repeated_samples_allowed_but_record_id_unique(tmp_path: Path) -> None:
    source = _csv(
        tmp_path,
        [
            "GW02,p,2026-01-01T00:00:00Z,1,GOOD,same",
            "GW02,p,2026-01-01T00:00:00Z,2,GOOD,same",
        ],
    )
    with pytest.raises(GeneratorError, match="invalid CSV line 3"):
        await _records(source, T0, T0 + timedelta(hours=1))


async def test_invalid_window_rejected(tmp_path: Path) -> None:
    source = _csv(tmp_path, ["GW02,p,2026-01-01T00:00:00Z,1,GOOD,r1"])
    with pytest.raises(GeneratorError):
        await _records(source, T0, T0)


async def test_exact_max_records_accepted_but_one_more_rejected(tmp_path: Path) -> None:
    rows = [f"GW02,p,2026-01-01T00:00:{i:02d}Z,1,GOOD,r{i}" for i in range(4)]
    source = _csv(tmp_path, rows)
    records = await _records(source, T0, T0 + timedelta(hours=1), max_records=4)
    assert len(records) == 4
    with pytest.raises(GeneratorError, match="capacity exceeded"):
        await _records(source, T0, T0 + timedelta(hours=1), max_records=3)
