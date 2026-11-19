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

    async def _process_partition(
        self, tp: TopicPartition, messages: list[KafkaRecord],
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Never have more than one in-flight batch per assigned partition."""
        async with semaphore:
            state = self._assignments.get(tp)
            if state is None or state.blocked or not messages:
                return
            generation = state.generation
            try:
                await self._ingestion.process(
                    generation=generation,
                    offsets=state.offsets,
                    records=messages,
                )
            except IngestionBlocked:
                if self._assignments.get(tp) is not state:
                    return
                state.blocked = True
                if tp in self._consumer.assignment():
                    self._consumer.pause(tp)
                _LOG.error(
                    "Storage Kafka partition blocked: topic=%s partition=%d",
                    tp.topic, tp.partition,
                )
            except (WriteUnconfirmed, OSError, TimeoutError):
                _LOG.warning(
                    "Storage Kafka write unconfirmed: topic=%s partition=%d",
                    tp.topic, tp.partition,
                )
                await asyncio.sleep(self._config.retry.initial_ms / 1000)
            except RuntimeError as exc:
                # Revocation can arrive during TDengine I/O. No old generation
                # response can be committed, but a normal revoke is not a
                # process-wide fatal error.
                if self._assignments.get(tp) is not state:
                    return
                raise
            if (
                self._assignments.get(tp) is not state
                or generation != state.generation
                or tp not in self._consumer.assignment()
            ):
                return
            # Kafka commits the next unread offset, only after durable readback.
            await self._consumer.commit({tp: state.offsets.next_commit_offset})
            if not state.blocked:
                # Replay any unconfirmed tail. The domain deduplicates content.
                self._consumer.seek(tp, state.offsets.next_commit_offset)
                state.offsets = PartitionOffsets(
                    partition=tp.partition,
                    generation=generation,
                    next_commit_offset=state.offsets.next_commit_offset,
                )

    async def run(self) -> None:
        """Parallelize partitions, while each partition remains strictly FIFO."""
        semaphore = asyncio.Semaphore(self._config.kafka.max_partition_workers)
        try:
            while not self._stop.is_set():
                batches = await self._consumer.getmany(
                    timeout_ms=self._config.tdengine.batch_wait_ms,
                    max_records=self._config.kafka.max_poll_records,
                )
                if not batches:
                    await asyncio.sleep(0)
                    continue
                bytes_received = 0
                workload: list[tuple[TopicPartition, list[KafkaRecord]]] = []
                for tp, batch in batches.items():
                    if not batch:
                        continue
                    records = [
                        KafkaRecord(
                            topic=message.topic,
                            partition=message.partition,
                            offset=message.offset,
                            key=message.key,
                            value=message.value,
                        )
                        for message in batch
                    ]
                    bytes_received += sum(len(record.value) for record in records)
                    workload.append((tp, records))
                if bytes_received > self._config.kafka.max_buffer_bytes:
                    # Fatal fail-closed: do not commit any Kafka offsets.
                    raise RuntimeError("Kafka fetched records exceed bounded Storage buffer")
                # One task per partition and one poll per iteration means
                # no duplicate in-flight writes to the same partition.
                async with asyncio.TaskGroup() as group:
                    for tp, records in workload:
                        group.create_task(
                            self._process_partition(tp, records, semaphore),
                            name=f"storage-partition-{tp.partition}",
                        )
        finally:
            await self._consumer.stop()

    async def retry_blocked_partition(
        self, *, request_id: str, topic: str, partition: int, expected_offset: int
    ) -> bool:
        """Accept a retry only when durable blocked evidence still matches."""
        if topic != self._config.kafka.topic or partition < 0:
            raise ValueError("partition is not part of this Storage consumer")
        accepted = self._metadata.request_partition_retry(
            request_id=request_id,
            topic=topic,
            partition=partition,
            expected_offset=expected_offset,
        )
        if not accepted:
            return False
        tp = TopicPartition(topic, partition)
        state = self._assignments.get(tp)
        if state is not None and state.blocked and tp in self._consumer.assignment():
            # Restart at the exact failed record. Never resume at the next offset.
            # Revisit any earlier not-yet-committed records before the bad record.
            self._consumer.seek(tp, state.offsets.next_commit_offset)
            state.offsets = PartitionOffsets(
                partition=partition,
                generation=state.generation,
                next_commit_offset=state.offsets.next_commit_offset,
            )
            state.blocked = False
            self._consumer.resume(tp)
        return True

    async def snapshot_progress(self) -> list[tuple[str, int, int, int, int, str]]:
        """Read broker end offsets for observed lag; never silently report zero lag."""
        assignments = list(self._assignments)
        if not assignments:
            return []
        last = await self._consumer.end_offsets(assignments)
        result: list[tuple[str, int, int, int, int, str]] = []
        for tp in assignments:
            entry = self._assignments.get(tp)
            if entry is None:
                continue
            next_offset = entry.offsets.next_commit_offset
            end_offset = last[tp]
            blocked = self._metadata.get_blocked_partition(tp.topic, tp.partition)
            result.append((
                tp.topic, tp.partition, next_offset, next_offset - 1,
                max(0, end_offset - next_offset), blocked[1] if blocked else "",
            ))
        return sorted(result)

    def request_stop(self) -> None:
        self._stop.set()

    async def stop(self) -> None:
        self._stop.set()
