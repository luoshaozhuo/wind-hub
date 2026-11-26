"""Ordered Kafka v3 ingestion: complete rows, last-offset-wins, safe commits."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.config import TypeMapping
from storage_service.application.ingestion import IngestionBlockedError, KafkaRecord
from storage_service.application.offsets import PartitionOffsets
from storage_service.application.wide_ingestion import WideIngestionService
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflictError,
)


def _mapping() -> TypeMapping:
    return TypeMapping(
        bool="BOOL", int8="TINYINT", uint8="TINYINT UNSIGNED",
        int16="SMALLINT", uint16="SMALLINT UNSIGNED",
        int32="INT", uint32="INT UNSIGNED", int64="BIGINT",
        uint64="BIGINT UNSIGNED", float32="FLOAT", float64="DOUBLE",
        string="VARCHAR", bytes="VARBINARY",
    )


def _record(offset: int, *, value: int, second: int = 0) -> KafkaRecord:
    payload = {
        "schema_version": 3,
        "wind_farm_id": "farm",
        "device_config_id": "device_wtg_v1.0.0",
        "device_id": "wtg001",
        "data_level": "protocol",
        "sample_time": f"2026-11-25T12:00:{second:02d}Z",
        "points": {"power": [value, 1], "wind_speed": [None, 0]},
    }
    return KafkaRecord(
        topic="history", partition=2, offset=offset,
        key=b"farm:wtg001",
        value=json.dumps(payload).encode(),
    )


def _service() -> tuple[WideIngestionService, Mock, Mock, Mock]:
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0", point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"), PhysicalPoint("wind_speed", "FLOAT")),
        max_row_bytes=65536,
    )
    backend, resolver, evidence, metrics = Mock(), Mock(), Mock(), Mock()
    backend.ensure_wide_table = AsyncMock()
    backend.insert_wide_batch = AsyncMock()
    resolver.resolve_wide_schema = AsyncMock(return_value=schema)
    resolver.wide_device_type.return_value = "wind"
    service = WideIngestionService(
        storage=backend, resolver=resolver, evidence=evidence,
        metrics=metrics, allowed_wind_farms=frozenset({"farm"}),
        type_mapping=_mapping(), max_batch_rows=1000, max_batch_bytes=8388608,
    )
    return service, backend, evidence, metrics


@pytest.mark.asyncio
async def test_duplicate_timestamp_last_kafka_offset_wins() -> None:
    service, backend, evidence, metrics = _service()
    offsets = PartitionOffsets(partition=2, generation=4, next_commit_offset=0)
    result = await service.process(
        generation=4, offsets=offsets,
        records=[_record(0, value=1), _record(1, value=2), _record(2, value=3, second=1)],
    )
    assert result.next_commit_offset == 3
    assert result.accepted_records == 3
    rows = backend.insert_wide_batch.await_args.args[1]
    assert tuple(row.values[0] for row in rows) == (2, 3)
    assert backend.insert_wide_batch.await_count == 1
    metrics.record_wide_persisted.assert_called_once_with(rows=2, points_per_row=2)
    evidence.block_partition.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_partial_record_blocks_without_offset_confirmation() -> None:
    service, backend, evidence, _metrics = _service()
    offsets = PartitionOffsets(partition=2, generation=5, next_commit_offset=0)
    record = _record(0, value=1)
    payload = json.loads(record.value)
    del payload["points"]["wind_speed"]
    bad = KafkaRecord(record.topic, record.partition, record.offset,
                      record.key, json.dumps(payload).encode())
    with pytest.raises(IngestionBlockedError):
        await service.process(generation=5, offsets=offsets, records=[bad])
    assert offsets.next_commit_offset == 0
    backend.insert_wide_batch.assert_not_awaited()
    evidence.block_partition.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_write_blocks_and_fences_offset() -> None:
    service, backend, evidence, metrics = _service()
    backend.insert_wide_batch.side_effect = TimeoutError()
    offsets = PartitionOffsets(partition=2, generation=6, next_commit_offset=0)
    with pytest.raises(IngestionBlockedError):
        await service.process(
            generation=6, offsets=offsets,
            records=[_record(0, value=1)],
        )
    assert offsets.next_commit_offset == 0
    metrics.record_unknown.assert_called_once()
    evidence.block_partition.assert_called_once()


@pytest.mark.asyncio
async def test_deterministic_schema_conflict_is_not_unknown_io() -> None:
    service, backend, evidence, metrics = _service()
    backend.ensure_wide_table.side_effect = TDengineSchemaConflictError(
        "immutable physical PointTable conflicts with persisted schema"
    )
    offsets = PartitionOffsets(partition=2, generation=8, next_commit_offset=0)
    with pytest.raises(TDengineSchemaConflictError):
        await service.process(
            generation=8, offsets=offsets,
            records=[_record(0, value=10)],
        )
    assert offsets.next_commit_offset == 0
    backend.insert_wide_batch.assert_not_awaited()
    evidence.block_partition.assert_not_called()
    metrics.record_unknown.assert_not_called()


@pytest.mark.parametrize(
    "rows, bytes_limit",
    [(1001, 8388608), (1000, 8388609)],
)
def test_ingestion_rejects_out_of_contract_stmt2_batch_limits(
    rows: int, bytes_limit: int,
) -> None:
    valid, backend, evidence, metrics = _service()
    with pytest.raises(ValueError, match="bounded authorized resources"):
        WideIngestionService(
            storage=backend,
            resolver=valid._resolver,
            evidence=evidence,
            metrics=metrics,
            allowed_wind_farms=frozenset({"farm"}),
            type_mapping=_mapping(),
            max_batch_rows=rows,
            max_batch_bytes=bytes_limit,
        )


@pytest.mark.asyncio
async def test_physical_byte_budget_splits_rows_even_when_json_is_small() -> None:
    _default, backend, evidence, metrics = _service()
    resolver = Mock()
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(
            PhysicalPoint("power", "INT"),
            PhysicalPoint("wind_speed", "VARBINARY(3000)"),
        ),
        max_row_bytes=65536,
    )
    assert schema.declared_row_bytes > 2000
    resolver.resolve_wide_schema = AsyncMock(return_value=schema)
    resolver.wide_device_type.return_value = "wind"
    service = WideIngestionService(
        storage=backend, resolver=resolver, evidence=evidence,
        metrics=metrics, allowed_wind_farms=frozenset({"farm"}),
        type_mapping=_mapping(), max_batch_rows=1000, max_batch_bytes=4096,
    )
    offsets = PartitionOffsets(partition=2, generation=9, next_commit_offset=0)
    result = await service.process(
        generation=9, offsets=offsets,
        records=[
            _record(0, value=1),
            _record(1, value=2, second=1),
            _record(2, value=3, second=2),
        ],
    )
    assert result.next_commit_offset == 3
    assert backend.insert_wide_batch.await_count == 3
    assert all(
        len(call.args[1]) == 1
        for call in backend.insert_wide_batch.await_args_list
    )
    evidence.block_partition.assert_not_called()


@pytest.mark.asyncio
async def test_single_physical_row_exceeding_batch_limit_blocks_offset() -> None:
    _default, backend, evidence, metrics = _service()
    resolver = Mock()
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(
            PhysicalPoint("power", "INT"),
            PhysicalPoint("wind_speed", "VARBINARY(5000)"),
        ),
        max_row_bytes=65536,
    )
    resolver.resolve_wide_schema = AsyncMock(return_value=schema)
    resolver.wide_device_type.return_value = "wind"
    service = WideIngestionService(
        storage=backend, resolver=resolver, evidence=evidence,
        metrics=metrics, allowed_wind_farms=frozenset({"farm"}),
        type_mapping=_mapping(), max_batch_rows=1000, max_batch_bytes=4096,
    )
    offsets = PartitionOffsets(partition=2, generation=10, next_commit_offset=0)
    with pytest.raises(IngestionBlockedError):
        await service.process(
            generation=10, offsets=offsets,
            records=[_record(0, value=1)],
        )
    assert offsets.next_commit_offset == 0
    backend.insert_wide_batch.assert_not_awaited()
    assert evidence.block_partition.call_args.args[3] == (
        "TDENGINE_WIDE_ROW_EXCEEDS_BATCH_BUDGET"
    )
