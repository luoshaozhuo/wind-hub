"""Device-wide quality filter contracts across signed history pages."""

from unittest.mock import AsyncMock

import pytest

from storage_service.application.wide_query import (
    WideCursorCodec,
    WideHistoryFilter,
    WideHistoryQueryService,
    WideHistoryRow,
)
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wind_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("wind_speed", "FLOAT"),),
        max_row_bytes=65536,
    )


def _filter(*, quality: int = 1, limit: int = 2) -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm_a",
        device_id="wtg_001",
        device_config_id="device_farm-a_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1,
        end_time_us=1000,
        point_ids=("wind_speed",),
        limit=limit,
        qualities=(quality,),
    )


def _row(timestamp: int, quality: PointQuality) -> WideHistoryRow:
    return WideHistoryRow(timestamp, (("wind_speed", float(timestamp), quality),))


@pytest.mark.asyncio
async def test_filtered_pages_do_not_skip_first_qualifying_lookahead() -> None:
    backend = AsyncMock()
    backend.fetch_wide_history.side_effect = [
        (_row(10, PointQuality.BAD), _row(20, PointQuality.GOOD)),
        (_row(30, PointQuality.GOOD), _row(40, PointQuality.GOOD)),
        (_row(30, PointQuality.GOOD),),
    ]
    service = WideHistoryQueryService(
        backend=backend, codec=WideCursorCodec(b"k" * 32)
    )
    original = _filter(limit=1)
    first = await service.query(
        schema=_schema(), filter_=original, device_type="wind"
    )
    assert [row.sample_time_us for row in first.rows] == [20]
    assert not first.complete
    second = await service.query(
        schema=_schema(), filter_=original, device_type="wind",
        page_token=first.next_page_token,
    )
    assert [row.sample_time_us for row in second.rows] == [30]
    # A cursor must advance past the last *consumed* record, not the lookahead.
    assert backend.fetch_wide_history.await_args.args[1].after_time_us == 20


def test_cursor_rejects_rebound_quality_predicate() -> None:
    codec = WideCursorCodec(b"k" * 32)
    token = codec.encode(
        _filter(), _schema(), last_us=20, horizon_us=1000
    )
    with pytest.raises(ValueError, match="unauthorized"):
        codec.decode(token, _filter(quality=2), _schema())


@pytest.mark.parametrize(
    "qualities, point_ids",
    [
        ((0, 4), ("wind_speed",)),
        ((True,), ("wind_speed",)),
        ((1, 1), ("wind_speed",)),
        ((1,), ("wind_speed", "other")),
    ],
)
def test_invalid_quality_predicates_are_rejected(
    qualities: tuple[int, ...], point_ids: tuple[str, ...],
) -> None:
    base = _filter()
    requested = WideHistoryFilter(
        wind_farm_id=base.wind_farm_id,
        device_id=base.device_id,
        device_config_id=base.device_config_id,
        data_level=base.data_level,
        start_time_us=base.start_time_us,
        end_time_us=base.end_time_us,
        point_ids=point_ids,
        limit=base.limit,
        qualities=qualities,
    )
    with pytest.raises(ValueError):
        requested.validate(_schema(), maximum=1000)


@pytest.mark.asyncio
async def test_filtered_query_rejects_corrupt_null_good_row() -> None:
    backend = AsyncMock()
    backend.fetch_wide_history.return_value = (
        WideHistoryRow(10, (("wind_speed", None, PointQuality.GOOD),)),
    )
    service = WideHistoryQueryService(
        backend=backend, codec=WideCursorCodec(b"k" * 32)
    )
    with pytest.raises(RuntimeError, match="NULL and quality"):
        await service.query(
            schema=_schema(), filter_=_filter(), device_type="wind"
        )


def test_cursor_cannot_advance_to_or_beyond_frozen_horizon() -> None:
    codec = WideCursorCodec(b"k" * 32)
    for last, horizon in ((100, 100), (101, 100), (100, 1_001)):
        with pytest.raises(ValueError, match="last < frozen horizon"):
            codec.encode(_filter(), _schema(), last_us=last, horizon_us=horizon)
