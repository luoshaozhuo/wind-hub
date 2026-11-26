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
    assert rows[0][8].startswith("p_") and rows[0][8].endswith("_v1_0_0")
    assert rows[1][10] == "'=HYPERLINK(1)"
    assert rows[1][9] == "1"
    assert rows[2][9] == "0"
    assert rows[2][11] == "2"
    assert rows[0][2] == "device_config_id"
    assert rows[1][2] == "device_farm_v1.0.0"
    assert rows[1][4] == "1.0.0"
    assert rows[1][6] == _schema().fingerprint


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


@pytest.mark.asyncio
async def test_wide_export_rejects_different_page_schema(tmp_path: Path) -> None:
    history = _history()
    page = history.query.return_value
    history.query.return_value = WideHistoryPage(
        rows=page.rows,
        complete=True,
        next_page_token="",
        schema_fingerprint="other-version-fingerprint",
    )
    with pytest.raises(RuntimeError, match="schema fingerprint"):
        await export_wide_device_history(
            history=history, schema=_schema(), filter_=_filter(),
            device_type="wind", artifact_id="wrong_schema",
            export_format="json", root=tmp_path, max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_wide_export_rejects_duplicate_timestamp_across_pages(
    tmp_path: Path,
) -> None:
    history = _history()
    fingerprint = _schema().fingerprint
    history.query.side_effect = [
        WideHistoryPage(
            rows=(WideHistoryRow(10, (
                ("power", 1.5, PointQuality.GOOD),
                ("status", "running", PointQuality.GOOD),
            )),),
            complete=False,
            next_page_token="next",
            schema_fingerprint=fingerprint,
        ),
        WideHistoryPage(
            rows=(WideHistoryRow(10, (
                ("power", 2.5, PointQuality.GOOD),
                ("status", "stopped", PointQuality.GOOD),
            )),),
            complete=True,
            next_page_token="",
            schema_fingerprint=fingerprint,
        ),
    ]
    with pytest.raises(RuntimeError, match="timestamp order"):
        await export_wide_device_history(
            history=history, schema=_schema(), filter_=_filter(),
            device_type="wind", artifact_id="duplicate",
            export_format="csv", root=tmp_path, max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_wide_export_rejects_inconsistent_quality_value(
    tmp_path: Path,
) -> None:
    history = _history()
    history.query.return_value = WideHistoryPage(
        rows=(WideHistoryRow(10, (
            ("power", 2.5, PointQuality.NOT_COLLECTED),
            ("status", None, PointQuality.BAD),
        )),),
        complete=True,
        next_page_token="",
        schema_fingerprint=_schema().fingerprint,
    )
    with pytest.raises(RuntimeError, match="NULL/quality"):
        await export_wide_device_history(
            history=history, schema=_schema(), filter_=_filter(),
            device_type="wind", artifact_id="corrupt",
            export_format="csv", root=tmp_path, max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_published_artifact_is_rolled_back_if_directory_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from storage_service.application import wide_export

    original_fsync = wide_export.os.fsync

    def fail_directory_sync(fd: int) -> None:
        import stat

        if stat.S_ISDIR(wide_export.os.fstat(fd).st_mode):
            raise OSError("directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(wide_export.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory fsync"):
        await export_wide_device_history(
            history=_history(), schema=_schema(), filter_=_filter(),
            device_type="wind", artifact_id="failed_sync",
            export_format="json", root=tmp_path, max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_wide_export_replay_gate_blocks_final_artifact(
    tmp_path: Path,
) -> None:
    history = _history()
    history.read_available.return_value = False
    with pytest.raises(RuntimeError, match="Kafka replay"):
        await export_wide_device_history(
            history=history, schema=_schema(), filter_=_filter(),
            device_type="wind", artifact_id="replay",
            export_format="json", root=tmp_path, max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []
