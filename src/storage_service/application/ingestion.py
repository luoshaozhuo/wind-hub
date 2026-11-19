"""Reliable sample ingestion: prove persistence before confirming Kafka offsets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from storage_service.application.offsets import PartitionOffsets
from storage_service.domain.sample import StorageSample


class StorageBackend(Protocol):
    """Port implemented by the TDengine adapter, never by Kafka or RPC code."""

    async def ensure_tables(self, samples: Sequence[StorageSample]) -> None:
        """Create/check only authorized, schema-verified objects."""

    async def fetch_content_hashes(
        self, samples: Sequence[StorageSample]
    ) -> Mapping[str, frozenset[str]]:
        """Return all persisted hashes for each logical_key, not arbitrary first rows."""

    async def insert_batch(self, samples: Sequence[StorageSample]) -> None:
        """Insert with stable composite keys; may fail after a partial actual write."""


class PointTableResolver(Protocol):
    """Fail closed when the exact historical immutable closure is unavailable."""

    async def require_exact_versions(self, samples: Sequence[StorageSample]) -> None:
        """Validate every point against its fixed PointTable and BusinessPoint versions."""


class PoisonEvidence(Protocol):
    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        """Persist raw bytes, content digest and the earliest blocked offset."""


class IngestionBlocked(RuntimeError):
    """A deterministic input or sample conflict requires operator intervention."""


class WriteUnconfirmed(RuntimeError):
    """Never advance offsets for a batch lacking complete durable evidence."""


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


class IngestionService:
    """Per-partition serial processing; commit remains the consumer's responsibility."""

    def __init__(
        self, *,
        storage: StorageBackend,
        point_tables: PointTableResolver,
        evidence: PoisonEvidence,
    ) -> None:
        self._storage = storage
        self._point_tables = point_tables
        self._evidence = evidence

    @staticmethod
    def _confirmed(
        samples: Sequence[StorageSample],
        observed: Mapping[str, frozenset[str]],
    ) -> bool:
        """No conflicting candidate is permitted; every row must match exactly."""
        return all(
            observed.get(sample.logical_key) == frozenset((sample.content_sha256,))
            for sample in samples
        )

    async def process(
        self,
        *,
        generation: int,
        offsets: PartitionOffsets,
        records: Sequence[KafkaRecord],
    ) -> IngestResult:
        """Process a partition FIFO. Do not consume past poison/UNKNOWN records."""
        accepted = 0
        replayed = 0
        for record in records:
            if record.partition != offsets.partition:
                raise ValueError("Kafka record is from a different partition")
            offsets.observe(generation=generation, offset=record.offset)
            try:
                sample = StorageSample.from_kafka_json(record.value)
                if record.key is None:
                    raise ValueError("Kafka record key is required")
                # The currently frozen Sink JSON v2 partition-key convention.
                # A future producer contract change must alter this validation.
                expected_key = (
                    f"{sample.wind_farm_id}:{sample.collector_id}:{sample.device_id}"
                ).encode("utf-8")
                if record.key != expected_key:
                    raise ValueError("Kafka key does not match stable producer key")
                await self._point_tables.require_exact_versions((sample,))
                await self._storage.ensure_tables((sample,))
                previous = await self._storage.fetch_content_hashes((sample,))
                hashes = previous.get(sample.logical_key, frozenset())
                if hashes and hashes != frozenset((sample.content_sha256,)):
                    raise IngestionBlocked("SAMPLE_CONFLICT")
                if hashes == frozenset((sample.content_sha256,)):
                    replayed += 1
                else:
                    try:
                        await self._storage.insert_batch((sample,))
                    except Exception as exc:
                        # Remote timeout/exception does not prove that INSERT failed.
                        observed = await self._storage.fetch_content_hashes((sample,))
                        if not self._confirmed((sample,), observed):
                            if observed.get(sample.logical_key):
                                raise IngestionBlocked("SAMPLE_CONFLICT") from exc
                            raise WriteUnconfirmed("TDengine result is UNKNOWN") from exc
                    observed = await self._storage.fetch_content_hashes((sample,))
                    if not self._confirmed((sample,), observed):
                        if observed.get(sample.logical_key):
                            raise IngestionBlocked("SAMPLE_CONFLICT")
                        raise WriteUnconfirmed("post-insert readback did not confirm row")
                accepted += 1
                offsets.confirm(generation=generation, offset=record.offset)
            except (ValueError, KeyError, IngestionBlocked) as exc:
                self._evidence.block_partition(
                    record.topic,
                    record.partition,
                    record.offset,
                    str(exc),
                    record.value,
                )
                raise IngestionBlocked(
                    f"{record.topic}:{record.partition}:{record.offset}: blocked"
                ) from exc
            # Transient errors deliberately propagate without advancing offsets.
        return IngestResult(
            next_commit_offset=offsets.next_commit_offset,
            accepted_records=accepted,
            replayed_records=replayed,
        )

