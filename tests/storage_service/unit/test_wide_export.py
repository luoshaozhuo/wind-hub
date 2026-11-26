"""Grouped CSV/JSON export preserves physical schema versions and quality."""

import csv
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.wide_group_export import (
    WideExportQuotaExceededError,
    export_grouped_wide_history,
)
from storage_service.application.wide_group_query import (
    GroupedWidePage,
    GroupedWideRows,
    GroupedWideSelector,
)
from storage_service.application.wide_query import WideHistoryRow
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel, WideIdentity
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.BUSINESS,
        points=(PhysicalPoint("power", "DOUBLE"), PhysicalPoint("status", "VARCHAR(64)")),
        max_row_bytes=65536,
    )


def _identity() -> WideIdentity:
    return WideIdentity("farm", "device_farm_v1.0.0", "wtg1", WideDataLevel.BUSINESS)


def _selector(*, schema_fingerprint: str = "") -> GroupedWideSelector:
    return GroupedWideSelector(
        wind_farm_id="farm",
        device_ids=("wtg1",),
        device_type="wind",
        point_table_id="point_table_wtg_v1.0.0",
        data_level=WideDataLevel.BUSINESS,
        start_time_us=1,
        end_time_us=1_000,
        point_ids=("power", "status"),
        qualities=(),
        schema_fingerprint=schema_fingerprint,
    )


def _rows() -> tuple[WideHistoryRow, ...]:
    return (
        WideHistoryRow(
            10,
            (
                ("power", 1.5, PointQuality.GOOD),
                ("status", "=HYPERLINK(1)", PointQuality.UNCERTAIN),
            ),
        ),
        WideHistoryRow(
            20,
            (
                ("power", None, PointQuality.NOT_COLLECTED),
                ("status", None, PointQuality.BAD),
            ),
        ),
    )


def _group(
    *,
    schema: WideSchema | None = None,
    rows: tuple[WideHistoryRow, ...] | None = None,
) -> GroupedWideRows:
    return GroupedWideRows(
        identity=_identity(),
        device_type="wind",
        schema=_schema() if schema is None else schema,
        point_ids=("power", "status"),
        rows=_rows() if rows is None else rows,
    )


def _history() -> Mock:
    history = Mock()
    history.query = AsyncMock(
        return_value=GroupedWidePage(
            groups=(_group(),),
            complete=True,
            next_page_token="",
        )
    )
    history.read_available.return_value = True
    return history


@pytest.mark.asyncio
async def test_atomic_grouped_csv_zip_with_versioned_columns_and_quality(
    tmp_path: Path,
) -> None:
    path, size, digest = await export_grouped_wide_history(
        history=_history(),
        selector=_selector(),
        artifact_id="test1",
        export_format="csv",
        root=tmp_path,
        max_bytes=1_000_000,
    )
    assert path == "test1.zip"
    assert size > 0
    assert len(digest) == 64
    with zipfile.ZipFile(tmp_path / path) as archive:
        assert set(archive.namelist()) == {"manifest.json", "group_0000.csv"}
        manifest = json.loads(archive.read("manifest.json"))
        rows = list(csv.reader(io.StringIO(archive.read("group_0000.csv").decode("utf-8"))))
    assert manifest[0]["point_table_id"] == "point_table_wtg_v1.0.0"
    assert manifest[0]["schema_fingerprint"] == _schema().fingerprint
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
async def test_atomic_grouped_json_returns_original_quality_and_null(tmp_path: Path) -> None:
    path, _size, _digest = await export_grouped_wide_history(
        history=_history(),
        selector=_selector(),
        artifact_id="test2",
        export_format="json",
        root=tmp_path,
        max_bytes=1_000_000,
    )
    doc = json.loads((tmp_path / path).read_text())
    assert len(doc["groups"]) == 1
    group = doc["groups"][0]
    assert group["point_table_version"] == "1.0.0"
    assert group["schema_fingerprint"] == _schema().fingerprint
    assert group["rows"][1]["points"]["power"] == [None, 0]
    assert group["rows"][1]["points"]["status"] == [None, 2]


@pytest.mark.asyncio
async def test_grouped_export_quota_failure_cannot_publish_partial_artifact(
    tmp_path: Path,
) -> None:
    with pytest.raises(WideExportQuotaExceededError):
        await export_grouped_wide_history(
            history=_history(),
            selector=_selector(),
            artifact_id="test3",
            export_format="json",
            root=tmp_path,
            max_bytes=20,
        )
    assert not (tmp_path / "test3.json").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_grouped_export_rejects_different_page_schema(tmp_path: Path) -> None:
    expected = _schema()
    wrong_schema = replace(expected, fingerprint="other-version-fingerprint")
    history = _history()
    history.query.return_value = GroupedWidePage(
        groups=(_group(schema=wrong_schema),),
        complete=True,
        next_page_token="",
    )
    with pytest.raises(RuntimeError, match="schema fingerprint"):
        await export_grouped_wide_history(
            history=history,
            selector=_selector(schema_fingerprint=expected.fingerprint),
            artifact_id="wrongschema",
            export_format="json",
            root=tmp_path,
            max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_grouped_export_rejects_duplicate_timestamp_across_pages(
    tmp_path: Path,
) -> None:
    history = _history()
    first_row = WideHistoryRow(
        10,
        (
            ("power", 1.5, PointQuality.GOOD),
            ("status", "running", PointQuality.GOOD),
        ),
    )
    duplicate_row = WideHistoryRow(
        10,
        (
            ("power", 2.5, PointQuality.GOOD),
            ("status", "stopped", PointQuality.GOOD),
        ),
    )
    history.query.side_effect = [
        GroupedWidePage(
            groups=(_group(rows=(first_row,)),),
            complete=False,
            next_page_token="next",
        ),
        GroupedWidePage(
            groups=(_group(rows=(duplicate_row,)),),
            complete=True,
            next_page_token="",
        ),
    ]
    with pytest.raises(RuntimeError, match="timestamp order"):
        await export_grouped_wide_history(
            history=history,
            selector=_selector(),
            artifact_id="duplicate",
            export_format="csv",
            root=tmp_path,
            max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_grouped_export_rejects_inconsistent_quality_value(
    tmp_path: Path,
) -> None:
    history = _history()
    corrupt_row = WideHistoryRow(
        10,
        (
            ("power", 2.5, PointQuality.NOT_COLLECTED),
            ("status", None, PointQuality.BAD),
        ),
    )
    history.query.return_value = GroupedWidePage(
        groups=(_group(rows=(corrupt_row,)),),
        complete=True,
        next_page_token="",
    )
    with pytest.raises(RuntimeError, match="invalid physical value"):
        await export_grouped_wide_history(
            history=history,
            selector=_selector(),
            artifact_id="corrupt",
            export_format="csv",
            root=tmp_path,
            max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_published_artifact_is_rolled_back_if_directory_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from storage_service.application import wide_group_export

    original_fsync = wide_group_export.os.fsync

    def fail_directory_sync(fd: int) -> None:
        import stat

        if stat.S_ISDIR(wide_group_export.os.fstat(fd).st_mode):
            raise OSError("directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(wide_group_export.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory fsync"):
        await export_grouped_wide_history(
            history=_history(),
            selector=_selector(),
            artifact_id="failedsync",
            export_format="json",
            root=tmp_path,
            max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_grouped_export_replay_gate_blocks_final_artifact(
    tmp_path: Path,
) -> None:
    history = _history()
    history.read_available.return_value = False
    with pytest.raises(RuntimeError, match="Kafka replay"):
        await export_grouped_wide_history(
            history=history,
            selector=_selector(),
            artifact_id="replay",
            export_format="json",
            root=tmp_path,
            max_bytes=1_000_000,
        )
    assert list(tmp_path.iterdir()) == []
