"""Manual-commit Kafka consumer: partition FIFO and generation-fenced commits."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiokafka import AIOKafkaConsumer, ConsumerRebalanceListener, TopicPartition

from storage_service.application.config import StorageConfig
from storage_service.application.ingestion import (
    IngestionBlocked,
    IngestionService,
    KafkaRecord,
    WriteUnconfirmed,
)
from storage_service.application.offsets import PartitionOffsets
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore


_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class _AssignedPartition:
    offsets: PartitionOffsets
    generation: int
    blocked: bool = False


class _PartitionListener(ConsumerRebalanceListener):
    def __init__(self, owner: KafkaStorageConsumer) -> None:
        self._owner = owner

    async def on_partitions_revoked(self, revoked: set[TopicPartition]) -> None:
        self._owner.revoke(revoked)

    async def on_partitions_assigned(self, assigned: set[TopicPartition]) -> None:
        await self._owner.assign(assigned)


class KafkaStorageConsumer:
    """Consumer is the only component allowed to perform Kafka Offset commits."""

    def __init__(
        self, *,
        configuration: StorageConfig,
        bootstrap_servers: str,
        ingestion: IngestionService,
        metadata: MetadataStore,
    ) -> None:
        if not bootstrap_servers:
            raise ValueError("Kafka bootstrap_servers is required")
        self._config = configuration
        self._ingestion = ingestion
        self._metadata = metadata
        self._consumer = AIOKafkaConsumer(
            bootstrap_servers=bootstrap_servers,
            group_id=configuration.kafka.group_id,
            enable_auto_commit=False,
            auto_offset_reset="none",
            max_poll_records=configuration.kafka.max_poll_records,
            fetch_max_bytes=configuration.kafka.max_buffer_bytes,
            max_partition_fetch_bytes=min(1048576, configuration.kafka.max_buffer_bytes),
        )
        self._assignments: dict[TopicPartition, _AssignedPartition] = {}
        self._generation = 0
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._consumer.subscribe(
            topics=[self._config.kafka.topic], listener=_PartitionListener(self)
        )
        await self._consumer.start()

    def revoke(self, revoked: set[TopicPartition]) -> None:
        """Fence old workers immediately; completion cannot commit old generation."""
        self._generation += 1
        for tp in revoked:
            previous = self._assignments.pop(tp, None)
            if previous is not None:
                previous.offsets.revoke(generation=previous.generation)

    async def assign(self, assigned: set[TopicPartition]) -> None:
        for tp in assigned:
            if tp in self._assignments:
                continue
            committed = await self._consumer.committed(tp)
            if committed is None:
                beginning = await self._consumer.beginning_offsets([tp])
                earliest = beginning[tp]
                # Initial group bootstrap is safe only when nothing was
                # previously trimmed; an existing offset gap is never reset.
                if earliest != 0:
                    raise RuntimeError("OFFSET_GAP: no committed offset and Kafka earliest > 0")
                self._consumer.seek(tp, 0)
                committed = 0
            current = _AssignedPartition(
                offsets=PartitionOffsets(
                    partition=tp.partition,
                    generation=self._generation,
                    next_commit_offset=committed,
                ),
                generation=self._generation,
            )
            block = self._metadata.get_blocked_partition(tp.topic, tp.partition)
            if block is not None:
                current.blocked = True
                self._consumer.pause(tp)
            self._assignments[tp] = current

    async def run(self) -> None:
        """Process real Kafka records, never treating delivery as persistence ACK."""
        try:
            while not self._stop.is_set():
                batches = await self._consumer.getmany(
                    timeout_ms=self._config.tdengine.batch_wait_ms,
                    max_records=self._config.kafka.max_poll_records,
                )
                if not batches:
                    await asyncio.sleep(0)
                    continue
                for tp, messages in batches.items():
                    state = self._assignments.get(tp)
                    if state is None or state.blocked or not messages:
                        continue
                    current_generation = state.generation
                    records = [
                        KafkaRecord(
                            topic=msg.topic,
                            partition=msg.partition,
                            offset=msg.offset,
                            key=msg.key,
                            value=msg.value,
                        )
                        for msg in messages
                    ]
                    if sum(len(record.value) for record in records) > self._config.kafka.max_buffer_bytes:
                        raise RuntimeError("Kafka fetched records exceed bounded Storage buffer")
                    try:
                        await self._ingestion.process(
                            generation=current_generation,
                            offsets=state.offsets,
                            records=records,
                        )
                    except IngestionBlocked:
                        state.blocked = True
                        if tp in self._consumer.assignment():
                            self._consumer.pause(tp)
                        _LOG.error(
                            "Storage Kafka partition blocked: topic=%s partition=%d",
                            tp.topic,
                            tp.partition,
                        )
                    except (WriteUnconfirmed, OSError, TimeoutError):
                        _LOG.warning(
                            "Storage Kafka write unconfirmed: topic=%s partition=%d",
                            tp.topic,
                            tp.partition,
                        )
                        await asyncio.sleep(self._config.retry.initial_ms / 1000)
                    if (
                        self._assignments.get(tp) is state
                        and state.generation == current_generation
                        and tp in self._consumer.assignment()
                    ):
                        # Commit the contiguous proved prefix (next-offset rule).
                        await self._consumer.commit({
                            tp: state.offsets.next_commit_offset
                        })
                        if not state.blocked:
                            # Rewind any unconfirmed tail; records may be written
                            # but not acknowledged, so stable sample keys dedup.
                            self._consumer.seek(tp, state.offsets.next_commit_offset)
                            state.offsets = PartitionOffsets(
                                partition=tp.partition,
                                generation=current_generation,
                                next_commit_offset=state.offsets.next_commit_offset,
                            )
        finally:
            await self._consumer.stop()

    def request_stop(self) -> None:
        self._stop.set()

    async def stop(self) -> None:
        self._stop.set()
