"""Exact wide-query cursors are signed, schema-scoped and order stable."""

from unittest.mock import AsyncMock

import pytest

from storage_service.application.wide_query import (
    WideCursorCodec,
    WideHistoryFilter,
    WideHistoryQueryService,
    WideHistoryRow,
)
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


def _schema(version: str = "1.0.0") -> WideSchema:
    return WideSchema.build(
        point_table_id=f"point_table_wtg_v{version}",
        point_table_version=version,
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"),),
        max_row_bytes=65536,
    )


def _filter(limit: int = 2) -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm",
        device_id="wtg001",
        device_config_id="device_wtg_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1,
        end_time_us=1000,
        point_ids=("power",),
        limit=limit,
    )


@pytest.mark.asyncio
async def test_wide_history_query_pages_without_duplicate_timestamps() -> None:
    schema, codec = _schema(), WideCursorCodec(b"x" * 32)
    backend = AsyncMock()
    backend.fetch_wide_history.side_effect = [
        (
            WideHistoryRow(10, (("power", 1, 1),)),
            WideHistoryRow(20, (("power", 2, 1),)),
            WideHistoryRow(30, (("power", 3, 1),)),
        ),
        (WideHistoryRow(30, (("power", 3, 1),)),),
    ]
    service = WideHistoryQueryService(backend=backend, codec=codec)
    first = await service.query(schema=schema, filter_=_filter(), device_type="wind")
    assert not first.complete
    assert tuple(row.sample_time_us for row in first.rows) == (10, 20)
    second = await service.query(
        schema=schema, filter_=_filter(), device_type="wind", page_token=first.next_page_token
    )
    assert second.complete
    assert tuple(row.sample_time_us for row in second.rows) == (30,)
    assert backend.fetch_wide_history.call_args.args[1].after_time_us == 20


@pytest.mark.asyncio
async def test_wide_cursor_must_match_exact_schema_version() -> None:
    codec = WideCursorCodec(b"x" * 32)
    token = codec.encode(_filter(), _schema(), last_us=10, horizon_us=999)
    with pytest.raises(ValueError, match="unauthorized"):
        codec.decode(token, _filter(), _schema("1.0.1"))


@pytest.mark.asyncio
async def test_backend_out_of_order_fails_closed() -> None:
    backend = AsyncMock()
    backend.fetch_wide_history.return_value = (
        WideHistoryRow(20, (("power", 2, 1),)),
        WideHistoryRow(10, (("power", 1, 1),)),
    )
    service = WideHistoryQueryService(backend=backend, codec=WideCursorCodec(b"x" * 32))
    with pytest.raises(RuntimeError, match="ordered timestamp"):
        await service.query(schema=_schema(), filter_=_filter(), device_type="wind")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "points, expected",
    [
        ((("power", 1, 0),), "NULL and quality"),
        ((("power", None, 5),), "invalid column"),
        ((("different", 1, 1),), "invalid column"),
        ((("power", 1, 4),), "invalid column"),
        ((), "incomplete physical"),
    ],
)
async def test_wide_query_rejects_malformed_physical_rows(
    points: tuple[tuple[str, object, int], ...], expected: str
) -> None:
    backend = AsyncMock()
    backend.fetch_wide_history.return_value = (WideHistoryRow(10, points),)
    service = WideHistoryQueryService(backend=backend, codec=WideCursorCodec(b"x" * 32))
    with pytest.raises(RuntimeError, match=expected):
        await service.query(schema=_schema(), filter_=_filter(), device_type="wind")


@pytest.mark.asyncio
async def test_wide_query_rejects_backend_row_from_other_version_field() -> None:
    backend = AsyncMock()
    backend.fetch_wide_history.return_value = (WideHistoryRow(10, (("power_v1_1_0", 11, 1),)),)
    service = WideHistoryQueryService(backend=backend, codec=WideCursorCodec(b"x" * 32))
    with pytest.raises(RuntimeError, match="invalid column"):
        await service.query(schema=_schema(), filter_=_filter(), device_type="wind")
