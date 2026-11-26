"""D5 frozen contract: explicit quality replaces the default GOOD selection."""

from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.wide_aggregate import WideAggregationService
from storage_service.application.wide_query import (
    WideHistoryFilter,
    WideHistoryPage,
    WideHistoryRow,
)
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", [2, 3])
@pytest.mark.parametrize("operation, expected", [("count", 1), ("min", 42)])
async def test_explicit_non_good_quality_participates_in_aggregate(
    quality: int, operation: str, expected: int
) -> None:
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"),),
        max_row_bytes=65536,
    )
    history = Mock()
    history.read_available.return_value = True
    history.query = AsyncMock(
        return_value=WideHistoryPage(
            rows=(WideHistoryRow(1000, (("power", 42, quality),)),),
            next_page_token="",
            complete=True,
            schema_fingerprint=schema.fingerprint,
        )
    )
    service = WideAggregationService(history=history, secret=b"x" * 32)
    page = await service.page(
        schema=schema,
        filter_=WideHistoryFilter(
            wind_farm_id="farm",
            device_id="wtg001",
            device_config_id="device_wtg_v1.0.0",
            data_level=WideDataLevel.PROTOCOL,
            start_time_us=1,
            end_time_us=2000,
            point_ids=("power",),
            qualities=(quality,),
            limit=10,
        ),
        device_type="wind",
        operation=operation,
        window_ms=1,
        page_size=10,
    )
    assert len(page.rows) == 1
    assert page.rows[0].good_count == 0
    assert page.rows[0].result == expected
