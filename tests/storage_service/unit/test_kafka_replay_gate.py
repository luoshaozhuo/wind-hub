"""Kafka history may be exposed only when assigned offset replay is settled."""

import pytest

from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)


@pytest.mark.asyncio
async def test_empty_assignment_does_not_expose_history_as_ready() -> None:
    consumer = object.__new__(KafkaStorageConsumer)
    consumer._assignments = {}
    consumer._retry_failures = {}
    consumer._replay_targets = {}
    consumer._assignment_ready = False

    await consumer.assign(set())

    assert consumer.recovering
    assert not consumer._assignment_ready
