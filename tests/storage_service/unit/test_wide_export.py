"""Wide CSV/JSON export preserves physical column versions and quality."""

import csv
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.wide_export import (
    WideExportQuotaExceededError,
    export_wide_device_history,
)
from storage_service.application.wide_query import (
    WideHistoryFilter,
    WideHistoryPage,
    WideHistoryRow,
)
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0", point_table_version="1.0.0",
        data_level=WideDataLevel.BUSINESS,
        points=(PhysicalPoint("power", "DOUBLE"), PhysicalPoint("status", "VARCHAR(64)")),
        max_row_bytes=65536,
    )


def _filter() -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm", device_id="wtg1",
        device_config_id="device_farm_v1.0.0",
        data_level=WideDataLevel.BUSINESS,
        start_time_us=1, end_time_us=1_000,
        point_ids=("power", "status"), limit=20,
    )


def _history() -> Mock:
    history = Mock()
    history.query = AsyncMock(return_value=WideHistoryPage(
        rows=(
            WideHistoryRow(10, (
                ("power", 1.5, PointQuality.GOOD),
                ("status", "=HYPERLINK(1)", PointQuality.UNCERTAIN),
            )),
            WideHistoryRow(20, (
                ("power", None, PointQuality.NOT_COLLECTED),
                ("status", None, PointQuality.BAD),
            )),
        ),
        complete=True, next_page_token="", schema_fingerprint=_schema().fingerprint,
    ))
    return history


@pytest.mark.asyncio
async def test_atomic_wide_csv_with_versioned_columns_and_quality(tmp_path: Path) -> None:
    path, size, digest = await export_wide_device_history(
        history=_history(), schema=_schema(), filter_=_filter(),
        device_type="wind", artifact_id="test1", export_format="csv",
        root=tmp_path, max_bytes=1_000_000,
    )
    assert path == "test1.csv"
    assert size > 0
    assert len(digest) == 64
    with (tmp_path / path).open(newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][1].startswith("p_") and rows[0][1].endswith("_v1_0_0")
    assert rows[1][3] == "'=HYPERLINK(1)"
    assert rows[1][2] == "1"
    assert rows[2][2] == "0"
    assert rows[2][4] == "2"


@pytest.mark.asyncio
async def test_atomic_wide_json_returns_original_quality_and_null(tmp_path: Path) -> None:
    path, _size, _digest = await export_wide_device_history(
        history=_history(), schema=_schema(), filter_=_filter(),
        device_type="wind", artifact_id="test2", export_format="json",
        root=tmp_path, max_bytes=1_000_000,
    )
    doc = json.loads((tmp_path / path).read_text())
    assert doc["point_table_version"] == "1.0.0"
    assert doc["rows"][1]["points"]["power"] == [None, 0]
    assert doc["rows"][1]["points"]["status"] == [None, 2]


@pytest.mark.asyncio
async def test_wide_export_quota_failure_cannot_publish_partial_artifact(
    tmp_path: Path,
) -> None:
    with pytest.raises(WideExportQuotaExceededError):
        await export_wide_device_history(
            history=_history(), schema=_schema(), filter_=_filter(),
            device_type="wind", artifact_id="test3", export_format="json",
            root=tmp_path, max_bytes=20,
        )
    assert not (tmp_path / "test3.json").exists()
    assert list(tmp_path.iterdir()) == []
