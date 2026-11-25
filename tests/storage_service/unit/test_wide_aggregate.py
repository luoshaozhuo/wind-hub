"""Wide aggregation preserves uint64 precision and explicit quality counters."""

from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.wide_aggregate import WideAggregationService
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
        data_level=WideDataLevel.PROTOCOL,
        points=(
            PhysicalPoint("counter", "BIGINT UNSIGNED"),
            PhysicalPoint("status", "INT"),
        ),
        max_row_bytes=65536,
    )


def _filter() -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm", device_id="wtg001",
        device_config_id="device_farm_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1, end_time_us=200_000_000,
        point_ids=("counter", "status"), limit=100,
    )


def _service() -> WideAggregationService:
    history = Mock()
    history.query = AsyncMock(return_value=WideHistoryPage(
        rows=(
            WideHistoryRow(1_000_000, (
                ("counter", 2**64 - 1, PointQuality.GOOD),
                ("status", 10, PointQuality.GOOD),
            )),
            WideHistoryRow(2_000_000, (
                ("counter", None, PointQuality.NOT_COLLECTED),
                ("status", 20, PointQuality.BAD),
            )),
            WideHistoryRow(62_000_000, (
                ("counter", 2**64 - 2, PointQuality.GOOD),
                ("status", 4, PointQuality.GOOD),
            )),
        ),
        next_page_token="", complete=True, schema_fingerprint=_schema().fingerprint,
    ))
    return WideAggregationService(history=history, secret=b"x" * 32)


@pytest.mark.asyncio
async def test_exact_uint64_max_preserved_across_window() -> None:
    service = _service()
    page = await service.page(
        schema=_schema(), filter_=_filter(), device_type="wind",
        operation="max", window_ms=60000, page_size=10,
    )
    values = [row for row in page.rows if row.point_id == "counter"]
    assert values[0].result == 2**64 - 1
    assert values[0].result_kind == 2
    assert values[0].good_count == 1
    assert values[0].missing_count == 1
    assert values[1].result == 2**64 - 2
    assert page.complete


@pytest.mark.asyncio
async def test_signed_aggregate_cursor_is_stable_and_cannot_be_rebound() -> None:
    service = _service()
    first = await service.page(
        schema=_schema(), filter_=_filter(), device_type="wind",
        operation="count", window_ms=60000, page_size=1,
    )
    assert not first.complete
    second = await service.page(
        schema=_schema(), filter_=_filter(), device_type="wind",
        operation="count", window_ms=60000, page_size=1,
        page_token=first.next_page_token,
    )
    assert second.rows != first.rows
    assert second.rows[0].result_kind == 2
    with pytest.raises(ValueError):
        await service.page(
            schema=_schema(), filter_=_filter(), device_type="wind",
            operation="max", window_ms=60000, page_size=1,
            page_token=first.next_page_token,
        )


@pytest.mark.asyncio
async def test_large_unsigned_avg_rejects_lossy_float_result() -> None:
    service = _service()
    with pytest.raises(ValueError, match="precision"):
        await service.page(
            schema=_schema(), filter_=_filter(), device_type="wind",
            operation="avg", window_ms=60000, page_size=10,
        )


@pytest.mark.asyncio
async def test_snapshot_cursor_rejects_changed_page_size() -> None:
    service = _service()
    first = await service.page(
        schema=_schema(), filter_=_filter(), device_type="wind",
        operation="count", window_ms=60000, page_size=1,
    )
    assert first.next_page_token
    with pytest.raises(ValueError, match="malformed"):
        await service.page(
            schema=_schema(), filter_=_filter(), device_type="wind",
            operation="count", window_ms=60000, page_size=2,
            page_token=first.next_page_token,
        )


@pytest.mark.asyncio
async def test_cached_aggregate_snapshot_fenced_during_replay() -> None:
    history = Mock()
    history.read_available.return_value = True
    history.query = AsyncMock(return_value=WideHistoryPage(
        rows=(
            WideHistoryRow(1_000_000, (
                ("counter", 3, PointQuality.GOOD),
                ("status", None, PointQuality.NOT_COLLECTED),
            )),
            WideHistoryRow(62_000_000, (
                ("counter", 4, PointQuality.GOOD),
                ("status", 1, PointQuality.GOOD),
            )),
        ),
        next_page_token="", complete=True, schema_fingerprint=_schema().fingerprint,
    ))
    service = WideAggregationService(history=history, secret=b"x" * 32)
    first = await service.page(
        schema=_schema(), filter_=_filter(), device_type="wind",
        operation="count", window_ms=60000, page_size=1,
    )
    history.read_available.return_value = False
    with pytest.raises(RuntimeError, match="replay"):
        await service.page(
            schema=_schema(), filter_=_filter(), device_type="wind",
            operation="count", window_ms=60000, page_size=1,
            page_token=first.next_page_token,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql_type, value",
    [
        ("BIGINT UNSIGNED", -1),
        ("BIGINT UNSIGNED", 2**64),
        ("INT", 2**31),
        ("BIGINT", -(2**63) - 1),
        ("BIGINT", float(12.0)),
        ("DOUBLE", float("nan")),
        ("DOUBLE", float("inf")),
    ],
)
async def test_wide_numeric_aggregate_rejects_invalid_physical_values(
    sql_type: str, value: object,
) -> None:
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("counter", sql_type),),
        max_row_bytes=65536,
    )
    filter_ = WideHistoryFilter(
        wind_farm_id="farm", device_id="wtg001",
        device_config_id="device_farm_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1, end_time_us=2_000_000,
        point_ids=("counter",), limit=100,
    )
    history = Mock()
    history.read_available.return_value = True
    history.query = AsyncMock(return_value=WideHistoryPage(
        rows=(WideHistoryRow(1_000_000, (
            ("counter", value, PointQuality.GOOD),
        )),),
        next_page_token="", complete=True, schema_fingerprint=schema.fingerprint,
    ))
    service = WideAggregationService(history=history, secret=b"x" * 32)
    with pytest.raises(ValueError, match="wide"):
        await service.page(
            schema=schema, filter_=filter_, device_type="wind",
            operation="max", window_ms=60000, page_size=10,
        )


@pytest.mark.asyncio
async def test_bad_uncertain_and_not_collected_are_excluded_from_good_count() -> None:
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"),),
        max_row_bytes=65536,
    )
    filter_ = WideHistoryFilter(
        wind_farm_id="farm", device_id="wtg001",
        device_config_id="device_farm_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1, end_time_us=10_000_000,
        point_ids=("power",), limit=20,
    )
    history = Mock()
    history.read_available.return_value = True
    history.query = AsyncMock(return_value=WideHistoryPage(
        rows=(
            WideHistoryRow(1_000_000, (("power", 5, PointQuality.GOOD),)),
            WideHistoryRow(2_000_000, (("power", 50, PointQuality.BAD),)),
            WideHistoryRow(3_000_000, (("power", 60, PointQuality.UNCERTAIN),)),
            WideHistoryRow(4_000_000, (("power", None, PointQuality.NOT_COLLECTED),)),
        ),
        next_page_token="", complete=True, schema_fingerprint=schema.fingerprint,
    ))
    service = WideAggregationService(history=history, secret=b"x" * 32)
    page = await service.page(
        schema=schema, filter_=filter_, device_type="wind",
        operation="avg", window_ms=60000, page_size=10,
    )
    assert len(page.rows) == 1
    row = page.rows[0]
    assert row.result == 5.0
    assert row.good_count == 1
    assert row.missing_count == 1
