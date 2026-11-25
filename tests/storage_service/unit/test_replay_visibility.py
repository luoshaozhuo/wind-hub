"""Offset replay quarantine prevents serving transient overwritten history."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from storage_service.application.query import (
    CursorCodec,
    HistoryFilter,
    HistoryQueryService,
    HistoryReplayUnavailableError,
)
from storage_service.domain.sample import DataLevel
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)


def _filter() -> HistoryFilter:
    return HistoryFilter(
        wind_farm_id="farm",
        data_level=DataLevel.PROTOCOL,
        start_us=1,
        end_us=1_000,
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
    service = HistoryQueryService(
        backend=backend,
        codec=CursorCodec(b"x" * 32),
        read_available=lambda: False,
    )
    with pytest.raises(HistoryReplayUnavailableError):
        await service.query(_filter())
    backend.fetch_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_discards_result_when_replay_starts_midread() -> None:
    started = asyncio.Event()
    resume = asyncio.Event()

    async def fetch_history(*_args: object, **_kwargs: object) -> tuple[()]:
        started.set()
        await resume.wait()
        return ()

    backend = AsyncMock()
    backend.fetch_history.side_effect = fetch_history
    safe = True
    service = HistoryQueryService(
        backend=backend,
        codec=CursorCodec(b"x" * 32),
        read_available=lambda: safe,
    )
    pending = asyncio.create_task(service.query(_filter()))
    await started.wait()
    safe = False
    resume.set()
    with pytest.raises(HistoryReplayUnavailableError):
        await pending
