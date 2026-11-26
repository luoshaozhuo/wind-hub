"""Offset replay quarantine prevents serving transient overwritten history."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from storage_service.application.wide_query import (
    WideCursorCodec,
    WideHistoryFilter,
    WideHistoryQueryService,
)
from storage_service.domain.errors import HistoryReplayUnavailableError
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"),),
        max_row_bytes=65536,
    )


def _filter() -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm",
        device_id="wtg001",
        device_config_id="device_wtg_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1,
        end_time_us=1000,
        point_ids=("power",),
        limit=2,
    )


def test_consumer_starts_without_authorized_history_visibility() -> None:
    consumer = object.__new__(KafkaStorageConsumer)
    consumer._assignment_ready = False
    consumer._replay_targets = {}
    assert consumer.recovering
    consumer._assignment_ready = True
    assert not consumer.recovering
    consumer._replay_targets = {"assigned:partition": 100}
    assert consumer.recovering


@pytest.mark.asyncio
async def test_query_rejected_when_replay_has_not_settled() -> None:
    backend = AsyncMock()
    service = WideHistoryQueryService(
        backend=backend,
        codec=WideCursorCodec(b"x" * 32),
        read_available=lambda: False,
    )
    with pytest.raises(HistoryReplayUnavailableError, match="replay"):
        await service.query(schema=_schema(), filter_=_filter(), device_type="wind")
    backend.fetch_wide_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_discards_result_when_replay_starts_midread() -> None:
    started = asyncio.Event()
    resume = asyncio.Event()

    async def fetch_wide_history(*_args: object, **_kwargs: object) -> tuple[()]:
        started.set()
        await resume.wait()
        return ()

    backend = AsyncMock()
    backend.fetch_wide_history.side_effect = fetch_wide_history
    safe = True
    service = WideHistoryQueryService(
        backend=backend,
        codec=WideCursorCodec(b"x" * 32),
        read_available=lambda: safe,
    )
    pending = asyncio.create_task(
        service.query(schema=_schema(), filter_=_filter(), device_type="wind")
    )
    await started.wait()
    safe = False
    resume.set()
    with pytest.raises(HistoryReplayUnavailableError, match="replay"):
        await pending
