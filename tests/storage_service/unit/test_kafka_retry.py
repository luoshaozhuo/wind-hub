"""Kafka 未确认写入的有界指数退避。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiokafka import TopicPartition

from storage_service.application.config import load_storage_config
from storage_service.application.ingestion import WriteUnconfirmedError
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
    _AssignedPartition,
)

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


class _Consumer:
    def __init__(self, tp: TopicPartition) -> None:
        self.tp = tp

    def assignment(self) -> set[TopicPartition]:
        return {self.tp}

    def pause(self, tp: TopicPartition) -> None:
        assert tp == self.tp

    async def commit(self, offsets: object) -> None:
        raise AssertionError("失败重试不得提交 offset")


class _Ingestion:
    async def process(self, **kwargs: object) -> None:
        raise WriteUnconfirmedError("injected unconfirmed write")


async def test_retry_uses_backoff_and_honors_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage_v1.0.1.yaml").read_bytes()
    )
    retry = config.retry.model_copy(update={"initial_ms": 500, "max_ms": 1000, "max_attempts": 2})
    config = config.model_copy(update={"retry": retry})
    tp = TopicPartition("topic", 0)
    monkeypatch.setattr(
        "storage_service.infrastructure.adapters.outbound.kafka_consumer.AIOKafkaConsumer",
        lambda **_kwargs: _Consumer(tp),
    )
    consumer = KafkaStorageConsumer(
        configuration=config,
        bootstrap_servers="127.0.0.1:9092",
        ingestion=cast(Any, _Ingestion()),
        metadata=cast(Any, SimpleNamespace()),
        metrics=StorageRuntimeMetrics(),
    )
    consumer._assignments[tp] = _AssignedPartition(
        offsets=PartitionOffsets(partition=0, generation=1, next_commit_offset=0),
        generation=1,
        blocked=False,
    )
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    for _ in range(2):
        await consumer._process_partition(tp, [cast(Any, object())], asyncio.Semaphore(1))
    with pytest.raises(WriteUnconfirmedError):
        await consumer._process_partition(tp, [cast(Any, object())], asyncio.Semaphore(1))
    assert delays == [0.5, 1.0]
    assert consumer._retry_failures[tp] == 3
