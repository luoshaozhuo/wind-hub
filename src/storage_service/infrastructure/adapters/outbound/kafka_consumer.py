"""Manual-commit Kafka consumer: partition FIFO and generation-fenced commits."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiokafka import AIOKafkaConsumer, ConsumerRebalanceListener, TopicPartition

from storage_service.application.config import StorageConfig
from storage_service.application.ingestion import (
    IngestionBlockedError,
    IngestionService,
    KafkaRecord,
    WriteUnconfirmedError,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflictError,
)

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class _AssignedPartition:
    offsets: PartitionOffsets
    generation: int
    blocked: bool = False


# aiokafka 未提供类型声明；运行时监听器实现其公开异步协议。
class _PartitionListener(ConsumerRebalanceListener):  # type: ignore[misc]
    def __init__(self, owner: KafkaStorageConsumer) -> None:
        self._owner = owner

    async def on_partitions_revoked(self, revoked: set[TopicPartition]) -> None:
        self._owner.revoke(revoked)

    async def on_partitions_assigned(self, assigned: set[TopicPartition]) -> None:
        await self._owner.assign(assigned)


class KafkaStorageConsumer:
    """Consumer is the only component allowed to perform Kafka Offset commits."""

    def __init__(
        self,
        *,
        configuration: StorageConfig,
        bootstrap_servers: str,
        ingestion: IngestionService,
        metadata: MetadataStore,
        metrics: StorageRuntimeMetrics,
    ) -> None:
        if not bootstrap_servers:
            raise ValueError("Kafka bootstrap_servers is required")
        self._config = configuration
        self._ingestion = ingestion
        self._metadata = metadata
        self._metrics = metrics
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
        self._retry_failures: dict[TopicPartition, int] = {}
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
            self._retry_failures.pop(tp, None)
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
                if block[0] < committed:
                    raise RuntimeError(
                        "blocked Kafka offset is older than committed cursor; "
                        "manual reconciliation is mandatory"
                    )
                current.blocked = True
                self._consumer.pause(tp)
            self._assignments[tp] = current

    async def _process_partition(
        self,
        tp: TopicPartition,
        messages: list[KafkaRecord],
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
            except IngestionBlockedError:
                if self._assignments.get(tp) is not state:
                    return
                state.blocked = True
                if tp in self._consumer.assignment():
                    self._consumer.pause(tp)
                _LOG.error(
                    "Storage Kafka partition blocked: topic=%s partition=%d",
                    tp.topic,
                    tp.partition,
                )
            except TDengineSchemaConflictError:
                self._metrics.record_schema_conflict()
                if self._assignments.get(tp) is not state:
                    return
                # A physical schema conflict belongs to the affected partition.
                # Preserve its earliest unconfirmed record as durable evidence,
                # rather than taking down unrelated wind farms and partitions.
                pending_offset = state.offsets.next_commit_offset
                first_pending = next(
                    (record for record in messages if record.offset == pending_offset), None
                )
                if first_pending is None:
                    raise RuntimeError("schema conflict has no provable Kafka offset") from None
                self._metadata.block_partition(
                    tp.topic,
                    tp.partition,
                    pending_offset,
                    "TDENGINE_SCHEMA_CONFLICT",
                    first_pending.value,
                )
                state.blocked = True
                if tp in self._consumer.assignment():
                    self._consumer.pause(tp)
                _LOG.error(
                    "Storage schema conflict blocked partition: topic=%s partition=%d offset=%d",
                    tp.topic,
                    tp.partition,
                    pending_offset,
                )
                return
            except (WriteUnconfirmedError, OSError, TimeoutError):
                failures = self._retry_failures.get(tp, 0) + 1
                self._retry_failures[tp] = failures
                _LOG.warning(
                    "Storage Kafka write unconfirmed: topic=%s partition=%d attempt=%d",
                    tp.topic,
                    tp.partition,
                    failures,
                )
                maximum = self._config.retry.max_attempts
                if maximum and failures > maximum:
                    raise
                delay_ms = min(
                    self._config.retry.max_ms,
                    self._config.retry.initial_ms * (2 ** (failures - 1)),
                )
                await asyncio.sleep(delay_ms / 1000)
                if (
                    self._assignments.get(tp) is state
                    and tp in self._consumer.assignment()
                    and not state.blocked
                ):
                    # getmany has already advanced the local fetch position.
                    # Rewind the *whole* uncommitted suffix before the next poll;
                    # the previous batch may have written partially or entirely.
                    # Readback deduplication makes replay safe.
                    cursor = state.offsets.next_commit_offset
                    self._consumer.seek(tp, cursor)
                    state.offsets = PartitionOffsets(
                        partition=tp.partition,
                        generation=generation,
                        next_commit_offset=cursor,
                    )
                return
            except RuntimeError:
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
            self._retry_failures.pop(tp, None)
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
            if expected_offset < state.offsets.next_commit_offset:
                raise RuntimeError("blocked retry cannot rewind before committed cursor")
            # Restart from contiguous prefix; never skip an unconfirmed record.
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
            result.append(
                (
                    tp.topic,
                    tp.partition,
                    next_offset,
                    next_offset - 1,
                    max(0, end_offset - next_offset),
                    blocked[1] if blocked else "",
                )
            )
        return sorted(result)

    def request_stop(self) -> None:
        self._stop.set()

    async def stop(self) -> None:
        self._stop.set()
        await self._consumer.stop()
