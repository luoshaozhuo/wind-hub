"""Reliable Kafka JSON v3 device-level Storage ingestion.

A Kafka message is one complete logical device row. All writes are ordered by
partition offset; only fully successful durable STMT2 operations confirm offsets.
No per-point reads, merges, or time-alignment take place here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn, Protocol

from storage_service.application.config import TypeMapping
from storage_service.application.ingestion import (
    ConfigHistoryUnavailableError,
    IngestResult,
    IngestionBlockedError,
    KafkaRecord,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.domain.wide_record import (
    WideIdentity,
    WideRecord,
    peek_wide_identity,
)
from storage_service.domain.wide_row import bind_wide_row
from storage_service.domain.wide_schema import WideSchema


class WideSchemaResolver(Protocol):
    async def resolve_wide_schema(
        self,
        record: WideIdentity,
        *,
        type_mapping: TypeMapping,
        max_row_bytes: int,
    ) -> WideSchema: ...


class WideBackend(Protocol):
    async def ensure_wide_table(
        self,
        schema: WideSchema,
        *,
        wind_farm_id: str,
        device_id: str,
        device_type: str,
    ) -> None: ...

    async def insert_wide_batch(
        self, schema: WideSchema, records: Sequence[WideRecord],
    ) -> None: ...


class BlockedRecordEvidence(Protocol):
    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _Validated:
    source: KafkaRecord
    row: WideRecord
    schema: WideSchema
    device_type: str


class WideIngestionService:
    """One Kafka partition processes complete rows in strictly increasing offset order."""

    def __init__(
        self,
        *,
        storage: WideBackend,
        resolver: WideSchemaResolver,
        evidence: BlockedRecordEvidence,
        metrics: StorageRuntimeMetrics,
        allowed_wind_farms: frozenset[str],
        type_mapping: TypeMapping,
        max_batch_rows: int,
        max_batch_bytes: int,
        max_row_bytes: int = 65536,
    ) -> None:
        if (
            not allowed_wind_farms or max_batch_rows < 1
            or max_batch_bytes < 4096 or max_row_bytes < 1024
        ):
            raise ValueError("wide ingestion requires bounded authorized resources")
        self._storage = storage
        self._resolver = resolver
        self._evidence = evidence
        self._metrics = metrics
        self._allowed_wind_farms = allowed_wind_farms
        self._mapping = type_mapping
        self._max_rows = max_batch_rows
        self._max_bytes = max_batch_bytes
        self._max_row_bytes = max_row_bytes

    def _block(self, record: KafkaRecord, reason: str) -> NoReturn:
        self._evidence.block_partition(
            record.topic, record.partition, record.offset, reason, record.value
        )
        raise IngestionBlockedError(
            f"Kafka device-wide partition blocked at "
            f"{record.topic}:{record.partition}:{record.offset}"
        )

    async def _resolve(self, source: KafkaRecord) -> _Validated:
        identity = peek_wide_identity(source.value)
        if identity.wind_farm_id not in self._allowed_wind_farms:
            raise ValueError("Kafka wind farm is not authorized for Storage")
        key = f"{identity.wind_farm_id}:{identity.device_id}".encode("utf-8")
        if source.key != key:
            raise ValueError("Kafka device key differs from immutable routing identity")
        schema = await self._resolver.resolve_wide_schema(
            identity, type_mapping=self._mapping, max_row_bytes=self._max_row_bytes
        )
        row = WideRecord.from_kafka_json(
            source.value,
            physical_points=tuple(column.point_id for column in schema.columns),
        )
        # Fail before DDL when any physical value is invalid or truncated.
        bind_wide_row(row, schema)
        # The immutable DeviceModel type is available from the resolved binding.
        device_type = self._resolver.wide_device_type(identity)
        return _Validated(source, row, schema, device_type)

    async def _flush(
        self,
        pending: Sequence[_Validated],
        *,
        offsets: PartitionOffsets,
        generation: int,
    ) -> int:
        """Persist consecutive same-device/schema runs without reordering offsets."""
        index = 0
        while index < len(pending):
            first = pending[index]
            end = index + 1
            key = (
                first.row.wind_farm_id,
                first.row.device_id,
                first.schema.fingerprint,
            )
            while end < len(pending):
                next_row = pending[end]
                next_key = (
                    next_row.row.wind_farm_id,
                    next_row.row.device_id,
                    next_row.schema.fingerprint,
                )
                if next_key != key:
                    break
                end += 1
            group = pending[index:end]
            await self._storage.ensure_wide_table(
                first.schema,
                wind_farm_id=first.row.wind_farm_id,
                device_id=first.row.device_id,
                device_type=first.device_type,
            )
            try:
                await self._storage.insert_wide_batch(
                    first.schema, tuple(entry.row for entry in group)
                )
            except TimeoutError:
                self._metrics.record_unknown()
                # A bounded driver call may still be running in a worker thread.
                # No retry is allowed in the same process after UNKNOWN.
                self._block(first.source, "TDENGINE_WIDE_IO_UNKNOWN")
            for item in group:
                offsets.confirm(generation=generation, offset=item.source.offset)
            self._metrics.record_persisted(len(group))
            index = end
        return len(pending)

    async def process(
        self,
        *,
        generation: int,
        offsets: PartitionOffsets,
        records: Sequence[KafkaRecord],
    ) -> IngestResult:
        pending: list[_Validated] = []
        pending_bytes = 0
        accepted = 0
        for source in records:
            if source.partition != offsets.partition:
                raise ValueError("Kafka record belongs to a different partition")
            if pending and (
                len(pending) >= self._max_rows
                or pending_bytes + len(source.value) > self._max_bytes
            ):
                accepted += await self._flush(
                    pending, offsets=offsets, generation=generation
                )
                pending.clear()
                pending_bytes = 0
            offsets.observe(generation=generation, offset=source.offset)
            if len(source.value) > self._max_bytes:
                self._block(source, "KAFKA_WIDE_RECORD_EXCEEDS_BATCH_BUDGET")
            try:
                validated = await self._resolve(source)
            except (ConfigHistoryUnavailableError, ValueError, TypeError, KeyError) as exc:
                if pending:
                    accepted += await self._flush(
                        pending, offsets=offsets, generation=generation
                    )
                self._block(source, f"WIDE_RECORD_INVALID: {exc}")
            pending.append(validated)
            pending_bytes += len(source.value)
        if pending:
            accepted += await self._flush(
                pending, offsets=offsets, generation=generation
            )
        return IngestResult(
            next_commit_offset=offsets.next_commit_offset,
            accepted_records=accepted,
            replayed_records=0,
        )
