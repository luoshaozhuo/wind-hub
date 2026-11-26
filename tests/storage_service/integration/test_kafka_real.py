"""Real Kafka commit timeout, replay and broker restart recovery."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from storage_service.application.config import load_storage_config
from storage_service.application.ingestion_contract import KafkaRecord
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.infrastructure.adapters.outbound.kafka_consumer import KafkaStorageConsumer
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from tests.fixtures.services import r5_stack

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.kafka,
    pytest.mark.fault_injection,
    pytest.mark.restart,
]


class _GatedIngestion:
    def __init__(self, *, gate_first: bool = True) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gate_first = gate_first
        self.calls: list[int] = []

    async def process(
        self,
        *,
        generation: int,
        offsets: PartitionOffsets,
        records: Sequence[KafkaRecord],
    ) -> None:
        assert records
        self.calls.append(records[0].offset)
        if self.gate_first and len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        for record in records:
            offsets.observe(generation=generation, offset=record.offset)
            offsets.confirm(generation=generation, offset=record.offset)


async def _wait_offset(
    admin: AIOKafkaAdminClient,
    group: str,
    tp: TopicPartition,
    expected: int,
    timeout: float = 60,
) -> None:
    deadline = time.monotonic() + timeout
    latest: Any = None
    while time.monotonic() < deadline:
        try:
            offsets = await admin.list_consumer_group_offsets(group, partitions=[tp])
            latest = offsets.get(tp)
            if latest is not None and latest.offset == expected:
                return
        except Exception as exc:  # broker restart is the injected fault
            latest = exc
        await asyncio.sleep(0.2)
    raise AssertionError(f"Kafka committed offset did not reach {expected}: {latest}")


@pytest.mark.asyncio
async def test_commit_timeout_replay_restart_and_rebalance_recovery(tmp_path: Path) -> None:
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable")
    stack = await asyncio.to_thread(r5_stack.up_stack)
    token = uuid4().hex[:10]
    topic = f"storage-commit-{token}"
    group = f"storage-commit-{token}"
    tp = TopicPartition(topic, 0)
    admin = AIOKafkaAdminClient(bootstrap_servers=stack.kafka)
    producer = AIOKafkaProducer(bootstrap_servers=stack.kafka)
    metadata = MetadataStore(tmp_path / "storage.sqlite3")
    metadata.open()
    ingestion = _GatedIngestion()
    consumer: KafkaStorageConsumer | None = None
    runner: asyncio.Task[None] | None = None
    replacement: KafkaStorageConsumer | None = None
    replacement_runner: asyncio.Task[None] | None = None
    replacement_metadata = MetadataStore(tmp_path / "replacement.sqlite3")
    replacement_ingestion = _GatedIngestion(gate_first=False)
    try:
        await admin.start()
        await admin.create_topics([NewTopic(name=topic, num_partitions=1, replication_factor=1)])
        await producer.start()
        await producer.send_and_wait(topic, b'{"record":0}', key=b"farm:collector:device")
        base = load_storage_config(
            (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
        )
        config = base.model_copy(
            update={
                "kafka": base.kafka.model_copy(
                    update={
                        "bootstrap_servers": stack.kafka,
                        "topic": topic,
                        "group_id": group,
                        "max_partition_workers": 1,
                        "commit_timeout_ms": 100,
                    }
                ),
                "retry": base.retry.model_copy(
                    update={"initial_ms": 100, "max_ms": 1000, "max_attempts": 0}
                ),
            }
        )
        consumer = KafkaStorageConsumer(
            configuration=config,
            bootstrap_servers=stack.kafka,
            ingestion=ingestion,  # type: ignore[arg-type]
            metadata=metadata,
            metrics=StorageRuntimeMetrics(),
        )
        await consumer.start()
        runner = asyncio.create_task(consumer.run())
        await asyncio.wait_for(ingestion.started.wait(), 30)

        # Pause after DML confirmation but before the real broker commit. The
        # bounded commit becomes UNKNOWN and must rewind to the original cursor.
        await asyncio.to_thread(stack.pause, "kafka")
        ingestion.release.set()
        await asyncio.sleep(1)
        assert not runner.done()
        assert ingestion.calls == [0]
        await asyncio.to_thread(stack.unpause, "kafka")
        await _wait_offset(admin, group, tp, 1)
        assert ingestion.calls[:2] == [0, 0]

        # A real broker restart forces coordinator discovery and a new group
        # generation. Replay remains bounded and the next record is committed.
        await asyncio.to_thread(stack.stop_container, "kafka")
        await asyncio.to_thread(stack.start_container, "kafka")
        await _wait_offset(admin, group, tp, 1, timeout=90)
        await producer.send_and_wait(topic, b'{"record":1}', key=b"farm:collector:device")
        await _wait_offset(admin, group, tp, 2, timeout=90)
        assert ingestion.calls[-1] == 1
        assert not runner.done()

        # A second real group member forces rebalance. Once the old owner has
        # stopped, only the replacement may ingest and commit the next offset.
        replacement_metadata.open()
        replacement = KafkaStorageConsumer(
            configuration=config,
            bootstrap_servers=stack.kafka,
            ingestion=replacement_ingestion,  # type: ignore[arg-type]
            metadata=replacement_metadata,
            metrics=StorageRuntimeMetrics(),
        )
        await replacement.start()
        replacement_runner = asyncio.create_task(replacement.run())
        await asyncio.sleep(2)
        consumer.request_stop()
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 20)
        await consumer.stop()
        consumer = None
        runner = None
        await producer.send_and_wait(topic, b'{"record":2}', key=b"farm:collector:device")
        await _wait_offset(admin, group, tp, 3, timeout=60)
        assert replacement_ingestion.calls[-1] == 2
        assert 2 not in ingestion.calls
        assert not replacement_runner.done()
    finally:
        if consumer is not None:
            consumer.request_stop()
        if runner is not None:
            await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 20)
        if consumer is not None:
            await consumer.stop()
        if replacement is not None:
            replacement.request_stop()
        if replacement_runner is not None:
            await asyncio.wait_for(asyncio.gather(replacement_runner, return_exceptions=True), 20)
        if replacement is not None:
            await replacement.stop()
        await producer.stop()
        await admin.close()
        metadata.close()
        replacement_metadata.close()
        await asyncio.to_thread(r5_stack.down_stack, stack)
