"""Reliable Kafka JSON v3 device-level Storage ingestion.

A Kafka message is one complete logical device row. All writes are ordered by
partition offset; only fully successful durable STMT2 operations confirm offsets.
No per-point reads, merges, or time-alignment take place here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn, Protocol

from storage_service.application.config import TypeMapping
from storage_service.application.ingestion_contract import (
    ConfigHistoryUnavailableError,
    IngestionBlockedError,
    IngestResult,
    KafkaRecord,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.domain.errors import TDengineSchemaConflictError
from storage_service.domain.wide_record import (
    WideIdentity,
    WideRecord,
    parse_wide_envelope,
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

    def wide_device_type(self, identity: WideIdentity) -> str: ...


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
        self,
        schema: WideSchema,
        records: Sequence[WideRecord],
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
        writer_count: int = 1,
    ) -> None:
        if (
            not allowed_wind_farms
            or not 1 <= max_batch_rows <= 1000
            or not 4096 <= max_batch_bytes <= 8388608
            or max_row_bytes < 1024
            or type(writer_count) is not int
            or not 1 <= writer_count <= 32
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
        self._write_permits = asyncio.Semaphore(writer_count)

    def _block(self, record: KafkaRecord, reason: str) -> NoReturn:
        self._evidence.block_partition(
            record.topic, record.partition, record.offset, reason, record.value
        )
        raise IngestionBlockedError(
            f"Kafka device-wide partition blocked at "
            f"{record.topic}:{record.partition}:{record.offset}"
        )

    async def _resolve(self, source: KafkaRecord) -> _Validated:
        parsed = parse_wide_envelope(source.value)
        identity = parsed.identity
        if identity.wind_farm_id not in self._allowed_wind_farms:
            raise ValueError("Kafka wind farm is not authorized for Storage")
        key = f"{identity.wind_farm_id}:{identity.device_id}".encode()
        if source.key != key:
            raise ValueError("Kafka device key differs from immutable routing identity")
        schema = await self._resolver.resolve_wide_schema(
            identity, type_mapping=self._mapping, max_row_bytes=self._max_row_bytes
        )
        row = WideRecord.from_parsed(
            parsed,
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
        """Batch interleaved device rows while preserving partition offset order.

        No offset becomes confirmed until every grouped write has returned
        successfully. A partial write therefore replays the entire prefix.
        """
        groups: dict[tuple[str, str, str], list[_Validated]] = {}
        for entry in pending:
            key = (
                entry.row.wind_farm_id,
                entry.row.device_id,
                entry.schema.fingerprint,
            )
            groups.setdefault(key, []).append(entry)

        async def persist_group(group: list[_Validated]) -> tuple[int, int]:
            # Each physical device group is one ordered STMT2 writer; groups
            # for different devices may run concurrently within one process.
            async with self._write_permits:
                first = group[0]
                latest_by_time = {
                    entry.row.sample_time_us: (position, entry.row)
                    for position, entry in enumerate(group)
                }
                ordered_rows = tuple(
                    row for _position, row in sorted(latest_by_time.values())
                )
                await self._storage.ensure_wide_table(
                    first.schema,
                    wind_farm_id=first.row.wind_farm_id,
                    device_id=first.row.device_id,
                    device_type=first.device_type,
                )
                await self._storage.insert_wide_batch(first.schema, ordered_rows)
                return len(ordered_rows), len(first.schema.columns)

        # Await every in-flight physical write even if one fails. Canceling
        # sibling STMT2 operations would create additional UNKNOWN outcomes.
        results = await asyncio.gather(
            *(persist_group(group) for group in groups.values()),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            error = errors[0]
            if isinstance(error, TDengineSchemaConflictError):
                raise error
            if isinstance(error, (RuntimeError, TimeoutError, OSError)):
                self._metrics.record_unknown()
                self._block(
                    pending[0].source,
                    f"TDENGINE_WIDE_IO_UNKNOWN: {type(error).__name__}",
                )
            raise error

        # Commit no source offset until ALL physical groups have persisted.
        for entry in pending:
            offsets.confirm(generation=generation, offset=entry.source.offset)
        for result in results:
            if not isinstance(result, tuple):
                raise RuntimeError("wide batch returned an invalid physical result")
            rows, width = result
            self._metrics.record_wide_persisted(rows=rows, points_per_row=width)
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
        pending_physical_bytes = 0
        accepted = 0
        for source in records:
            if source.partition != offsets.partition:
                raise ValueError("Kafka record belongs to a different partition")
            if pending and (
                len(pending) >= self._max_rows
                or pending_bytes + len(source.value) > self._max_bytes
            ):
                accepted += await self._flush(pending, offsets=offsets, generation=generation)
                pending.clear()
                pending_bytes = 0
                pending_physical_bytes = 0
            offsets.observe(generation=generation, offset=source.offset)
            if len(source.value) > self._max_bytes:
                self._block(source, "KAFKA_WIDE_RECORD_EXCEEDS_BATCH_BUDGET")
            try:
                validated = await self._resolve(source)
            except (ConfigHistoryUnavailableError, ValueError, TypeError, KeyError) as exc:
                if pending:
                    accepted += await self._flush(pending, offsets=offsets, generation=generation)
                self._block(source, f"WIDE_RECORD_INVALID: {exc}")
            # Kafka payload size is not the STMT2 in-memory column budget.
            # Use the worst-case physical width from the pinned TDengine schema.
            row_bytes = validated.schema.declared_row_bytes
            if row_bytes > self._max_bytes:
                if pending:
                    accepted += await self._flush(pending, offsets=offsets, generation=generation)
                self._block(source, "TDENGINE_WIDE_ROW_EXCEEDS_BATCH_BUDGET")
            if pending and pending_physical_bytes + row_bytes > self._max_bytes:
                accepted += await self._flush(pending, offsets=offsets, generation=generation)
                pending.clear()
                pending_bytes = 0
                pending_physical_bytes = 0
            pending.append(validated)
            pending_bytes += len(source.value)
            pending_physical_bytes += row_bytes
        if pending:
            accepted += await self._flush(pending, offsets=offsets, generation=generation)
        return IngestResult(
            next_commit_offset=offsets.next_commit_offset,
            accepted_records=accepted,
            replayed_records=0,
        )
