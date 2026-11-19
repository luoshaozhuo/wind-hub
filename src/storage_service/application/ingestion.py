"""Reliable Kafka ingestion: partition FIFO, bounded batches, durable readback.

No Kafka offset is confirmed until all samples in that batch are proved
persisted without conflicting content for the same logical measurement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn, Protocol

from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.domain.sample import StorageSample


class StorageBackend(Protocol):
    """Outbound TDengine port; all methods have finite I/O deadlines."""

    async def ensure_tables(self, samples: Sequence[StorageSample]) -> None:
        """Create and verify only authorized table objects."""

    async def fetch_content_hashes(
        self, samples: Sequence[StorageSample]
    ) -> Mapping[str, frozenset[str]]:
        """Return all hashes by logical_key, not just one arbitrary row."""

    async def insert_batch(self, samples: Sequence[StorageSample]) -> None:
        """Insert a bounded group; partial durable writes may precede an error."""


class PointTableResolver(Protocol):
    """Exact immutable point table and business point validation."""

    async def require_exact_versions(self, samples: Sequence[StorageSample]) -> None:
        """Raise when the historical configuration cannot be proved."""


class PoisonEvidence(Protocol):
    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        """Persist original Kafka bytes before changing partition state."""


class ConfigHistoryUnavailable(RuntimeError):
    """Historical configuration is absent, corrupt, or cannot be validated."""


class IngestionBlocked(RuntimeError):
    """A deterministic bad message or sample conflict requires operator action."""


class WriteUnconfirmed(RuntimeError):
    """After UNKNOWN, at least one sample could not be verified as persisted."""


@dataclass(frozen=True, slots=True)
class KafkaRecord:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes


@dataclass(frozen=True, slots=True)
class IngestResult:
    next_commit_offset: int
    accepted_records: int
    replayed_records: int


@dataclass(frozen=True, slots=True)
class _Validated:
    record: KafkaRecord
    sample: StorageSample


class IngestionService:
    """Bounded DML batching while preserving Kafka partition order and fencing."""

    def __init__(
        self, *,
        storage: StorageBackend,
        point_tables: PointTableResolver,
        evidence: PoisonEvidence,
        metrics: StorageRuntimeMetrics,
        allowed_wind_farms: frozenset[str],
        max_batch_records: int = 1000,
        max_batch_bytes: int = 1048576,
    ) -> None:
        if max_batch_records < 1 or max_batch_bytes < 4096:
            raise ValueError("Storage DML batch bounds must be positive")
        self._storage = storage
        self._point_tables = point_tables
        self._evidence = evidence
        if not allowed_wind_farms:
            raise ValueError("Storage requires explicitly authorized wind farms")
        self._metrics = metrics
        self._allowed_wind_farms = allowed_wind_farms
        self._max_batch_records = max_batch_records
        self._max_batch_bytes = max_batch_bytes

    def _block(self, record: KafkaRecord, reason: str) -> NoReturn:
        self._evidence.block_partition(
            record.topic, record.partition, record.offset, reason, record.value
        )
        raise IngestionBlocked(
            f"Kafka partition blocked at {record.topic}:{record.partition}:{record.offset}"
        )

    @staticmethod
    def _decode(record: KafkaRecord) -> StorageSample:
        sample = StorageSample.from_kafka_json(record.value)
        if record.key is None:
            raise ValueError("Kafka message key is required")
        expected = (
            f"{sample.wind_farm_id}:{sample.collector_id}:{sample.device_id}"
        ).encode("utf-8")
        if record.key != expected:
            raise ValueError("Kafka message key differs from producer partition key")
        return sample

    async def _flush(
        self, batch: Sequence[_Validated], *,
        offsets: PartitionOffsets,
        generation: int,
    ) -> tuple[int, int]:
        """Validate, insert only missing unique rows, then verify all keys."""
        if not batch:
            return 0, 0

        # Conflicting content inside one in-flight group must not be inserted.
        seen: dict[str, str] = {}
        for index, entry in enumerate(batch):
            previous = seen.setdefault(
                entry.sample.logical_key, entry.sample.content_sha256
            )
            if previous != entry.sample.content_sha256:
                if index:
                    await self._flush(batch[:index], offsets=offsets, generation=generation)
                self._block(entry.record, "SAMPLE_CONFLICT_IN_BATCH")

        samples = tuple(item.sample for item in batch)
        for index, entry in enumerate(batch):
            try:
                await self._point_tables.require_exact_versions((entry.sample,))
            except ConfigHistoryUnavailable as exc:
                if index:
                    await self._flush(
                        batch[:index], offsets=offsets, generation=generation
                    )
                self._block(entry.record, f"CONFIG_HISTORY_UNAVAILABLE: {exc}")
        await self._storage.ensure_tables(samples)
        existing = await self._storage.fetch_content_hashes(samples)
        for index, entry in enumerate(batch):
            hashes = existing.get(entry.sample.logical_key, frozenset())
            if hashes and hashes != frozenset((entry.sample.content_sha256,)):
                if index:
                    await self._flush(batch[:index], offsets=offsets, generation=generation)
                self._block(entry.record, "SAMPLE_CONFLICT_PERSISTED")

        missing: dict[str, StorageSample] = {}
        replayed = 0
        for entry in batch:
            sample = entry.sample
            if existing.get(sample.logical_key) == frozenset((sample.content_sha256,)):
                replayed += 1
            elif sample.logical_key in missing:
                replayed += 1
            else:
                missing[sample.logical_key] = sample

        failed: Exception | None = None
        if missing:
            try:
                await self._storage.insert_batch(tuple(missing.values()))
            except Exception as exc:
                # UNKNOWN is not FAILURE: verify every durable row before progressing.
                self._metrics.record_unknown()
                failed = exc

        try:
            confirmed = await self._storage.fetch_content_hashes(samples)
        except Exception as exc:
            raise WriteUnconfirmed("cannot verify TDengine batch readback") from exc

        for index, entry in enumerate(batch):
            sample = entry.sample
            hashes = confirmed.get(sample.logical_key, frozenset())
            if hashes != frozenset((sample.content_sha256,)):
                if hashes:
                    # Never confirm earlier rows before resolving an ambiguous DML
                    # batch: unknown partial effects may have crossed the boundary.
                    self._block(entry.record, "SAMPLE_CONFLICT_AFTER_INSERT")
                raise WriteUnconfirmed("TDengine batch has unconfirmed records") from failed

        # Readback proves *every* logical key before a single offset advances.
        for entry in batch:
            offsets.confirm(generation=generation, offset=entry.record.offset)
        self._metrics.record_persisted(len(missing))
        return len(batch), replayed

    async def process(
        self, *,
        generation: int,
        offsets: PartitionOffsets,
        records: Sequence[KafkaRecord],
    ) -> IngestResult:
        accepted = 0
        replayed = 0
        pending: list[_Validated] = []
        bytes_pending = 0
        for record in records:
            if record.partition != offsets.partition:
                raise ValueError("Kafka record from wrong partition")
            if pending and (
                len(pending) >= self._max_batch_records
                or bytes_pending + len(record.value) > self._max_batch_bytes
            ):
                count, duplicates = await self._flush(
                    pending, offsets=offsets, generation=generation
                )
                accepted += count
                replayed += duplicates
                pending.clear()
                bytes_pending = 0
            offsets.observe(generation=generation, offset=record.offset)
            if len(record.value) > self._max_batch_bytes:
                self._block(record, "KAFKA_RECORD_EXCEEDS_BATCH_BYTES")
            try:
                sample = self._decode(record)
                if sample.wind_farm_id not in self._allowed_wind_farms:
                    raise ValueError("wind farm is not authorized for Storage ingestion")
            except (ValueError, KeyError, TypeError) as exc:
                if pending:
                    count, duplicates = await self._flush(
                        pending, offsets=offsets, generation=generation
                    )
                    accepted += count
                    replayed += duplicates
                self._block(record, str(exc))
            pending.append(_Validated(record=record, sample=sample))
            bytes_pending += len(record.value)

        if pending:
            count, duplicates = await self._flush(
                pending, offsets=offsets, generation=generation
            )
            accepted += count
            replayed += duplicates
        return IngestResult(
            next_commit_offset=offsets.next_commit_offset,
            accepted_records=accepted,
            replayed_records=replayed,
        )
