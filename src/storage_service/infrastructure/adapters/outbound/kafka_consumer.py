"""Manual-commit Kafka consumer: partition FIFO and generation-fenced commits."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from aiokafka import AIOKafkaConsumer, ConsumerRebalanceListener, TopicPartition
from aiokafka.errors import KafkaError

from storage_service.application.config import StorageConfig
from storage_service.application.ingestion_contract import (
    IngestionBlockedError,
    KafkaRecord,
    WriteUnconfirmedError,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.application.wide_ingestion import WideIngestionService
from storage_service.domain.errors import TDengineSchemaConflictError
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore

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
        ingestion: WideIngestionService,
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
            auto_offset_reset="earliest",
            max_poll_records=configuration.kafka.max_poll_records,
            fetch_max_bytes=min(
                configuration.tdengine.ingestion.batch_max_bytes,
                configuration.tdengine.ingestion.buffer_max_bytes // 2,
            ),
            max_partition_fetch_bytes=min(
                1048576,
                configuration.tdengine.ingestion.batch_max_bytes,
                configuration.tdengine.ingestion.buffer_max_bytes // 2
            ),
        )
        self._assignments: dict[TopicPartition, _AssignedPartition] = {}
        self._retry_failures: dict[TopicPartition, int] = {}
        self._generation = 0
        self._epoch_id = uuid.uuid4().hex
        self._stop = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._consumer_closed = False
        self._assignment_ready = False
        # Snapshot each assigned partition's broker end before serving queries.
        self._replay_targets: dict[TopicPartition, int] = {}

    @property
    def recovering(self) -> bool:
        """Suspend user-facing reads until every assigned replay prefix is committed."""
        return not self._assignment_ready or bool(self._replay_targets)

    async def start(self) -> None:
        if self._consumer_closed:
            raise RuntimeError("closed Kafka consumer cannot be restarted")
        self._consumer.subscribe(
            topics=[self._config.kafka.topic], listener=_PartitionListener(self)
        )
        await self._consumer.start()

    def revoke(self, revoked: set[TopicPartition]) -> None:
        """Fence old workers immediately; completion cannot commit old generation."""
        self._generation += 1
        for tp in revoked:
            self._retry_failures.pop(tp, None)
            self._replay_targets.pop(tp, None)
            previous = self._assignments.pop(tp, None)
            if previous is not None:
                previous.offsets.revoke(generation=previous.generation)
        if not self._assignments:
            self._assignment_ready = False

    async def assign(self, assigned: set[TopicPartition]) -> None:
        for tp in assigned:
            if tp in self._assignments:
                continue
            committed = await self._consumer.committed(tp)
            beginning = await self._consumer.beginning_offsets([tp])
            earliest = beginning[tp]
            end = await self._consumer.end_offsets([tp])
            if committed is not None and committed > end[tp]:
                raise RuntimeError("OFFSET_GAP: committed offset exceeds Kafka log end")
            if committed is None:
                # Initial group bootstrap is safe only when nothing was
                # previously trimmed; an existing offset gap is never reset.
                if earliest != 0:
                    raise RuntimeError("OFFSET_GAP: no committed offset and Kafka earliest > 0")
                committed = 0
            elif committed < earliest:
                raise RuntimeError("OFFSET_GAP: committed offset is older than Kafka earliest")
            # Reassignment must discard the previous owner's uncommitted fetch
            # position; otherwise its already-polled suffix can skip records.
            self._consumer.seek(tp, committed)
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
            if committed < end[tp]:
                self._replay_targets[tp] = end[tp]
        # Empty assignment must never advertise settled Kafka history.
        self._assignment_ready = bool(self._assignments)

    async def _retry_unconfirmed(
        self,
        tp: TopicPartition,
        *,
        state: _AssignedPartition,
        generation: int,
        cursor: int,
        error: Exception,
    ) -> None:
        if (
            self._stop.is_set()
            or self._assignments.get(tp) is not state
            or generation != state.generation
            or tp not in self._consumer.assignment()
        ):
            return
        failures = self._retry_failures.get(tp, 0) + 1
        self._retry_failures[tp] = failures
        _LOG.warning(
            "Storage Kafka write/commit unconfirmed: topic=%s partition=%d attempt=%d",
            tp.topic,
            tp.partition,
            failures,
        )
        maximum = self._config.retry.max_attempts
        if maximum and failures > maximum:
            raise error
        delay_ms = min(
            self._config.retry.max_ms,
            self._config.retry.initial_ms * (2 ** min(failures - 1, 32)),
        )
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), delay_ms / 1000)
        if (
            not self._stop.is_set()
            and self._assignments.get(tp) is state
            and generation == state.generation
            and tp in self._consumer.assignment()
            and not state.blocked
        ):
            # getmany has already advanced the local fetch position. Rewind the
            # whole uncommitted suffix; readback deduplication makes replay safe.
            self._consumer.seek(tp, cursor)
            state.offsets = PartitionOffsets(
                partition=tp.partition,
                generation=generation,
                next_commit_offset=cursor,
            )

    async def _process_partition(
        self,
        tp: TopicPartition,
        messages: list[KafkaRecord],
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Expose in-flight Kafka buffer gauges even while awaiting a permit."""
        bytes_received = sum(len(message.value) for message in messages)
        self._metrics.change_buffer(rows=len(messages), byte_count=bytes_received)
        try:
            await self._process_partition_inner(tp, messages, semaphore)
        finally:
            self._metrics.change_buffer(rows=-len(messages), byte_count=-bytes_received)

    async def _process_partition_inner(
        self,
        tp: TopicPartition,
        messages: list[KafkaRecord],
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Never have more than one in-flight batch per assigned partition."""
        async with semaphore:
            state = self._assignments.get(tp)
            if self._stop.is_set() or state is None or state.blocked or not messages:
                return
            generation = state.generation
            retry_cursor = state.offsets.next_commit_offset
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
                return
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
            except (WriteUnconfirmedError, OSError, TimeoutError) as error:
                await self._retry_unconfirmed(
                    tp,
                    state=state,
                    generation=generation,
                    cursor=state.offsets.next_commit_offset,
                    error=error,
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
                self._stop.is_set()
                or self._assignments.get(tp) is not state
                or generation != state.generation
                or tp not in self._consumer.assignment()
            ):
                return
            # Kafka commits the next unread offset, only after durable readback.
            try:
                async with asyncio.timeout(self._config.kafka.commit_timeout_ms / 1000):
                    await self._consumer.commit({tp: state.offsets.next_commit_offset})
            except (KafkaError, OSError, TimeoutError) as error:
                # The write is confirmed but the commit is not. Replay from the
                # cursor captured before this batch, not from its successful end.
                await self._retry_unconfirmed(
                    tp,
                    state=state,
                    generation=generation,
                    cursor=retry_cursor,
                    error=error,
                )
                return
            # The commit itself awaits broker I/O. Ownership may have changed
            # while it was in flight, so fence every subsequent local mutation.
            if (
                self._stop.is_set()
                or self._assignments.get(tp) is not state
                or generation != state.generation
                or tp not in self._consumer.assignment()
            ):
                return
            self._retry_failures.pop(tp, None)
            self._metrics.record_confirmed_records(state.offsets.next_commit_offset - retry_cursor)
            target = self._replay_targets.get(tp)
            if target is not None and state.offsets.next_commit_offset >= target:
                self._replay_targets.pop(tp, None)
            if not state.blocked:
                # getmany has already advanced the fetch position. Seeking a
                # fully confirmed batch discards useful prefetched records.
                next_offset = state.offsets.next_commit_offset
                if next_offset != messages[-1].offset + 1:
                    self._consumer.seek(tp, next_offset)
                    state.offsets = PartitionOffsets(
                        partition=tp.partition,
                        generation=generation,
                        next_commit_offset=next_offset,
                    )

    async def run(self) -> None:
        """Poll ahead into a bounded FIFO while preserving per-partition offset order.

        One queue consumer processes whole poll groups in fetch order. Each group
        retains the existing partition-level parallelism and generation fences.
        """
        limits = self._config.tdengine.ingestion
        max_rows = limits.buffer_max_rows
        max_bytes = limits.buffer_max_bytes
        poll_rows = self._config.kafka.max_poll_records
        poll_bytes = min(limits.batch_max_bytes, max_bytes // 2)
        semaphore = asyncio.Semaphore(self._config.kafka.max_partition_workers)
        queue: asyncio.Queue[
            tuple[list[tuple[TopicPartition, _AssignedPartition, list[KafkaRecord]]], int, int]
            | None
        ] = asyncio.Queue(maxsize=max_rows)
        stop_waiter = asyncio.create_task(self._stop.wait(), name="storage-stop-waiter")
        space_available = asyncio.Event()
        buffered_rows = 0
        buffered_bytes = 0

        async def process_queue() -> None:
            nonlocal buffered_rows, buffered_bytes
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    return
                workload, rows, byte_count = item
                try:
                    async with asyncio.TaskGroup() as group:
                        for tp, owner, records in workload:
                            current = self._assignments.get(tp)
                            if (
                                current is not owner
                                or current.blocked
                                or records[0].offset != current.offsets.next_commit_offset
                            ):
                                # An old generation or an unconfirmed rewind must
                                # not process the already-fetched suffix.
                                continue
                            group.create_task(
                                self._process_partition(tp, records, semaphore),
                                name=f"storage-partition-{tp.partition}",
                            )
                finally:
                    buffered_rows -= rows
                    buffered_bytes -= byte_count
                    space_available.set()
                    queue.task_done()

        worker = asyncio.create_task(process_queue(), name="storage-kafka-dispatch")
        poll: asyncio.Task[Any] | None = None
        try:
            while not self._stop.is_set():
                while (
                    buffered_rows + poll_rows > max_rows
                    or buffered_bytes + poll_bytes > max_bytes
                ):
                    space_available.clear()
                    wake = asyncio.create_task(space_available.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (wake, worker, stop_waiter),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if worker in done:
                            await worker
                            raise RuntimeError("Storage Kafka dispatch exited unexpectedly")
                        if stop_waiter in done:
                            break
                    finally:
                        wake.cancel()
                        with suppress(asyncio.CancelledError):
                            await wake
                if self._stop.is_set():
                    break
                poll = asyncio.create_task(
                    self._consumer.getmany(
                        timeout_ms=limits.batch_wait_ms,
                        max_records=poll_rows,
                    ),
                    name="storage-kafka-poll",
                )
                try:
                    completed, _ = await asyncio.wait(
                        (poll, worker, stop_waiter),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if worker in completed:
                        await worker
                        raise RuntimeError("Storage Kafka dispatch exited unexpectedly")
                    if stop_waiter in completed:
                        if poll.done() and not poll.cancelled():
                            poll.exception()
                        break
                    batches = await poll
                finally:
                    if not poll.done():
                        poll.cancel()
                        with suppress(asyncio.CancelledError):
                            await poll
                    poll = None
                workload: list[tuple[TopicPartition, _AssignedPartition, list[KafkaRecord]]] = []
                rows_received = 0
                bytes_received = 0
                for tp, batch in batches.items():
                    owner = self._assignments.get(tp)
                    if not batch or owner is None or owner.blocked:
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
                    rows_received += len(records)
                    bytes_received += sum(len(record.value) for record in records)
                    workload.append((tp, owner, records))
                if not workload:
                    continue
                if rows_received > max_rows or bytes_received > max_bytes:
                    raise RuntimeError("Kafka fetched records exceed bounded Storage buffer")
                # Kafka can exceed fetch_max_bytes for the first large message.
                # Before admitting such a poll, wait for older groups to drain.
                while (
                    buffered_rows + rows_received > max_rows
                    or buffered_bytes + bytes_received > max_bytes
                ):
                    space_available.clear()
                    wake = asyncio.create_task(space_available.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (wake, worker, stop_waiter),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if worker in done:
                            await worker
                            raise RuntimeError("Storage Kafka dispatch exited unexpectedly")
                        if stop_waiter in done:
                            break
                    finally:
                        wake.cancel()
                        with suppress(asyncio.CancelledError):
                            await wake
                if self._stop.is_set():
                    break
                queue.put_nowait((workload, rows_received, bytes_received))
                buffered_rows += rows_received
                buffered_bytes += bytes_received
        finally:
            stop_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await stop_waiter
            if poll is not None and not poll.done():
                poll.cancel()
                with suppress(asyncio.CancelledError):
                    await poll
            try:
                if not worker.done():
                    await queue.put(None)
                    await worker
                elif not worker.cancelled():
                    # Retrieve a dispatcher exception without masking the
                    # original failure from the polling loop.
                    worker.exception()
            finally:
                await self._close_consumer()

    async def _close_consumer(self) -> None:
        """Serialize shutdown; the run loop and external STOP share one owner."""
        async with self._close_lock:
            if not self._consumer_closed:
                await self._consumer.stop()
                self._consumer_closed = True

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
            self._retry_failures.pop(tp, None)
            self._consumer.resume(tp)
        return True

    async def snapshot_progress(
        self,
    ) -> list[tuple[str, int, int, int, int, str, int | None, str]]:
        """Read broker end offsets for observed lag; never silently report zero lag."""
        assignments = list(self._assignments)
        if not assignments:
            return []
        last = await self._consumer.end_offsets(assignments)
        result: list[tuple[str, int, int, int, int, str, int | None, str]] = []
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
                    blocked[0] if blocked else None,
                    f"{self._epoch_id}:{entry.generation}",
                )
            )
        return sorted(result)

    def request_stop(self) -> None:
        self._stop.set()
        self.revoke(set(self._assignments))

    async def stop(self) -> None:
        self.request_stop()
        await self._close_consumer()
