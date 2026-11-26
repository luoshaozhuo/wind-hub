"""Wide history metrics track active work and release leases after errors."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.wide_query import (
    WideCursorCodec,
    WideHistoryFilter,
    WideHistoryQueryService,
)
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"),),
        max_row_bytes=65536,
    )


def _request() -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm-a",
        device_id="wtg001",
        device_config_id="device_farm_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1,
        end_time_us=2_000_000,
        point_ids=("power",),
        limit=10,
    )


@pytest.mark.asyncio
async def test_wide_query_reports_inflight_and_releases_slot() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_verification(*args: object, **kwargs: object) -> None:
        entered.set()
        await release.wait()

    backend = Mock()
    backend.verify_existing_wide_table = AsyncMock(side_effect=block_verification)
    backend.fetch_wide_history = AsyncMock(return_value=())
    service = WideHistoryQueryService(backend=backend, codec=WideCursorCodec(b"x" * 32))
    task = asyncio.create_task(
        service.query(schema=_schema(), filter_=_request(), device_type="wind")
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert service.active_queries == 1
    finally:
        release.set()
    page = await task
    assert page.complete
    assert service.active_queries == 0


@pytest.mark.asyncio
async def test_wide_query_error_releases_active_count() -> None:
    backend = Mock()
    backend.verify_existing_wide_table = AsyncMock(return_value=None)
    backend.fetch_wide_history = AsyncMock(side_effect=RuntimeError("backend unavailable"))
    service = WideHistoryQueryService(backend=backend, codec=WideCursorCodec(b"x" * 32))
    with pytest.raises(RuntimeError, match="backend unavailable"):
        await service.query(schema=_schema(), filter_=_request(), device_type="wind")
    assert service.active_queries == 0
