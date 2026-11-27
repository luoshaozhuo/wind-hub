"""Ordered Kafka v3 ingestion: complete rows, last-offset-wins, safe commits."""

import asyncio
import json
from collections.abc import Sequence
from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.config import TypeMapping
from storage_service.application.ingestion_contract import IngestionBlockedError, KafkaRecord
from storage_service.application.offsets import PartitionOffsets
from storage_service.application.wide_ingestion import WideIngestionService
from storage_service.domain.wide_record import WideDataLevel, WideIdentity, WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflictError,
)


def _mapping() -> TypeMapping:
    return TypeMapping(
        bool="BOOL",
        int8="TINYINT",
        uint8="TINYINT UNSIGNED",
        int16="SMALLINT",
        uint16="SMALLINT UNSIGNED",
        int32="INT",
        uint32="INT UNSIGNED",
        int64="BIGINT",
        uint64="BIGINT UNSIGNED",
        float32="FLOAT",
        float64="DOUBLE",
        string="VARCHAR",
        bytes="VARBINARY",
    )


def _record(
    offset: int,
    *,
    value: int,
    second: int = 0,
    device: str = "wtg001",
    device_config_id: str = "device_wtg_v1.0.0",
) -> KafkaRecord:
    payload = {
        "schema_version": 3,
        "wind_farm_id": "farm",
        "device_config_id": device_config_id,
        "device_id": device,
        "data_level": "protocol",
        "sample_time": f"2026-11-25T12:00:{second:02d}Z",
        "points": {"power": [value, 1], "wind_speed": [None, 0]},
    }
    return KafkaRecord(
        topic="history",
        partition=2,
        offset=offset,
        key=f"farm:{device}".encode(),
        value=json.dumps(payload).encode(),
    )


def _service(*, writer_count: int = 1) -> tuple[WideIngestionService, Mock, Mock, Mock]:
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
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
        storage=backend,
        resolver=resolver,
        evidence=evidence,
        metrics=metrics,
        allowed_wind_farms=frozenset({"farm"}),
        type_mapping=_mapping(),
        max_batch_rows=1000,
        max_batch_bytes=8388608,
        writer_count=writer_count,
    )
    return service, backend, evidence, metrics


@pytest.mark.asyncio
async def test_duplicate_timestamp_last_kafka_offset_wins() -> None:
    service, backend, evidence, metrics = _service()
    offsets = PartitionOffsets(partition=2, generation=4, next_commit_offset=0)
    result = await service.process(
        generation=4,
        offsets=offsets,
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
    bad = KafkaRecord(
        record.topic, record.partition, record.offset, record.key, json.dumps(payload).encode()
    )
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
            generation=6,
            offsets=offsets,
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
            generation=8,
            offsets=offsets,
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
    rows: int,
    bytes_limit: int,
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
        storage=backend,
        resolver=resolver,
        evidence=evidence,
        metrics=metrics,
        allowed_wind_farms=frozenset({"farm"}),
        type_mapping=_mapping(),
        max_batch_rows=1000,
        max_batch_bytes=4096,
    )
    offsets = PartitionOffsets(partition=2, generation=9, next_commit_offset=0)
    result = await service.process(
        generation=9,
        offsets=offsets,
        records=[
            _record(0, value=1),
            _record(1, value=2, second=1),
            _record(2, value=3, second=2),
        ],
    )
    assert result.next_commit_offset == 3
    assert backend.insert_wide_batch.await_count == 3
    assert all(len(call.args[1]) == 1 for call in backend.insert_wide_batch.await_args_list)
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
        storage=backend,
        resolver=resolver,
        evidence=evidence,
        metrics=metrics,
        allowed_wind_farms=frozenset({"farm"}),
        type_mapping=_mapping(),
        max_batch_rows=1000,
        max_batch_bytes=4096,
    )
    offsets = PartitionOffsets(partition=2, generation=10, next_commit_offset=0)
    with pytest.raises(IngestionBlockedError):
        await service.process(
            generation=10,
            offsets=offsets,
            records=[_record(0, value=1)],
        )
    assert offsets.next_commit_offset == 0
    backend.insert_wide_batch.assert_not_awaited()
    assert evidence.block_partition.call_args.args[3] == ("TDENGINE_WIDE_ROW_EXCEEDS_BATCH_BUDGET")


def _vary_schema_by_device(service: WideIngestionService) -> None:
    """Force distinct immutable schema families for concurrency-oriented tests."""
    base = service._resolver.resolve_wide_schema.return_value
    schemas: dict[str, WideSchema] = {}

    async def resolve(identity: WideIdentity, **_kwargs: object) -> WideSchema:
        if identity.device_id not in schemas:
            schemas[identity.device_id] = WideSchema.build(
                point_table_id=f"{base.point_table_id}_{identity.device_id}",
                point_table_version=base.point_table_version,
                data_level=base.data_level,
                points=(PhysicalPoint("power", "INT"), PhysicalPoint("wind_speed", "FLOAT")),
                max_row_bytes=65536,
            )
        return schemas[identity.device_id]

    service._resolver.resolve_wide_schema.side_effect = resolve


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_count", [1, 2, 4, 8])
async def test_writer_count_bounds_parallel_group_writes(writer_count: int) -> None:
    service, backend, _evidence, _metrics = _service(writer_count=writer_count)
    _vary_schema_by_device(service)
    group_count = writer_count * 2
    release = asyncio.Event()
    first_wave_started = asyncio.Event()
    active = 0
    peak = 0

    async def insert(_schema: WideSchema, rows: Sequence[WideRecord]) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == writer_count:
            first_wave_started.set()
        try:
            await release.wait()
        finally:
            active -= 1

    backend.insert_wide_batch.side_effect = insert
    offsets = PartitionOffsets(partition=2, generation=11, next_commit_offset=0)
    task = asyncio.create_task(
        service.process(
            generation=11,
            offsets=offsets,
            records=[
                _record(offset, value=offset, device=f"wtg{offset:03d}")
                for offset in range(group_count)
            ],
        )
    )
    try:
        await asyncio.wait_for(first_wave_started.wait(), timeout=2)
        started_before_release = backend.insert_wide_batch.await_count
        active_before_release = active
    finally:
        release.set()
    result = await asyncio.wait_for(task, timeout=2)

    assert started_before_release == writer_count
    assert active_before_release == writer_count
    assert peak == writer_count
    assert backend.insert_wide_batch.await_count == group_count
    assert result.next_commit_offset == group_count
    assert result.accepted_records == group_count


@pytest.mark.asyncio
async def test_mixed_devices_and_schemas_group_deduplicate_and_parallelize() -> None:
    service, backend, _evidence, _metrics = _service(writer_count=2)
    schema_v1 = service._resolver.resolve_wide_schema.return_value
    schema_v2 = WideSchema.build(
        point_table_id="point_table_wtg_v2.0.0",
        point_table_version="2.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("power", "INT"), PhysicalPoint("wind_speed", "FLOAT")),
        max_row_bytes=65536,
    )

    async def resolve(identity: WideIdentity, **_kwargs: object) -> WideSchema:
        if identity.device_config_id == "device_wtg_v2.0.0":
            return schema_v2
        return schema_v1

    service._resolver.resolve_wide_schema.side_effect = resolve
    release = asyncio.Event()
    first_wave_started = asyncio.Event()
    active = 0
    peak = 0

    async def insert(_schema: WideSchema, _rows: Sequence[WideRecord]) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            first_wave_started.set()
        try:
            await release.wait()
        finally:
            active -= 1

    backend.insert_wide_batch.side_effect = insert
    offsets = PartitionOffsets(partition=2, generation=12, next_commit_offset=0)
    task = asyncio.create_task(
        service.process(
            generation=12,
            offsets=offsets,
            records=[
                _record(0, value=1),
                _record(1, value=2),
                _record(2, value=3, second=1),
                _record(3, value=4, device="wtg002"),
                _record(4, value=5, device_config_id="device_wtg_v2.0.0"),
            ],
        )
    )
    try:
        await asyncio.wait_for(first_wave_started.wait(), timeout=2)
        started_before_release = backend.insert_wide_batch.await_count
    finally:
        release.set()
    result = await asyncio.wait_for(task, timeout=2)

    grouped = {
        call.args[0].fingerprint: call.args[1] for call in backend.insert_wide_batch.await_args_list
    }
    original_group = grouped[schema_v1.fingerprint]
    assert tuple(row.values[0] for row in original_group) == (2, 3, 4)
    assert {row.device_id for row in original_group} == {"wtg001", "wtg002"}
    assert len(grouped) == 2
    assert grouped[schema_v2.fingerprint][0].device_id == "wtg001"
    assert started_before_release == 2
    assert peak == 2
    assert backend.insert_wide_batch.await_count == 2
    assert backend.ensure_wide_table.await_count == 3
    assert result.next_commit_offset == 5


@pytest.mark.asyncio
async def test_one_failed_write_waits_for_all_started_peer_io_without_commit() -> None:
    service, backend, evidence, metrics = _service(writer_count=3)
    _vary_schema_by_device(service)
    all_started = asyncio.Event()
    failure_raised = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()
    completed: set[str] = set()
    cleaned: set[str] = set()

    async def insert(_schema: WideSchema, rows: Sequence[WideRecord]) -> None:
        device = rows[0].device_id
        started.add(device)
        if len(started) == 3:
            all_started.set()
        await all_started.wait()
        try:
            if device == "wtg000":
                failure_raised.set()
                raise OSError("physical write failed")
            await release.wait()
            completed.add(device)
        finally:
            cleaned.add(device)

    backend.insert_wide_batch.side_effect = insert
    offsets = PartitionOffsets(partition=2, generation=13, next_commit_offset=0)
    task = asyncio.create_task(
        service.process(
            generation=13,
            offsets=offsets,
            records=[
                _record(offset, value=offset, device=f"wtg{offset:03d}") for offset in range(3)
            ],
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=2)
    await asyncio.wait_for(failure_raised.wait(), timeout=2)
    waited_for_peers = not task.done()
    release.set()
    with pytest.raises(IngestionBlockedError):
        await asyncio.wait_for(task, timeout=2)

    assert waited_for_peers
    assert started == {"wtg000", "wtg001", "wtg002"}
    assert completed == {"wtg001", "wtg002"}
    assert cleaned == started
    assert offsets.next_commit_offset == 0
    metrics.record_wide_persisted.assert_not_called()
    metrics.record_unknown.assert_called_once_with()
    evidence.block_partition.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_write_precedes_concurrent_schema_conflict_without_commit() -> None:
    service, backend, evidence, metrics = _service(writer_count=3)
    _vary_schema_by_device(service)
    all_started = asyncio.Event()
    started: set[str] = set()
    succeeded: set[str] = set()

    async def insert(_schema: WideSchema, rows: Sequence[WideRecord]) -> None:
        device = rows[0].device_id
        started.add(device)
        if len(started) == 3:
            all_started.set()
        await all_started.wait()
        if device == "wtg001":
            raise TimeoutError("write outcome unknown")
        if device == "wtg002":
            raise TDengineSchemaConflictError("immutable schema conflict")
        succeeded.add(device)

    backend.insert_wide_batch.side_effect = insert
    offsets = PartitionOffsets(partition=2, generation=14, next_commit_offset=0)
    with pytest.raises(IngestionBlockedError):
        await asyncio.wait_for(
            service.process(
                generation=14,
                offsets=offsets,
                records=[
                    _record(offset, value=offset, device=f"wtg{offset:03d}") for offset in range(3)
                ],
            ),
            timeout=2,
        )

    assert succeeded == {"wtg000"}
    assert offsets.next_commit_offset == 0
    metrics.record_unknown.assert_called_once_with()
    metrics.record_wide_persisted.assert_not_called()
    evidence.block_partition.assert_called_once()
    assert evidence.block_partition.call_args.args[3] == ("TDENGINE_WIDE_IO_UNKNOWN: TimeoutError")


@pytest.mark.asyncio
async def test_cancellation_cleans_every_started_write_without_commit() -> None:
    service, backend, evidence, metrics = _service(writer_count=3)
    _vary_schema_by_device(service)
    all_started = asyncio.Event()
    never_finish = asyncio.Event()
    started: set[str] = set()
    cleaned: set[str] = set()

    async def insert(_schema: WideSchema, rows: Sequence[WideRecord]) -> None:
        device = rows[0].device_id
        started.add(device)
        if len(started) == 3:
            all_started.set()
        try:
            await never_finish.wait()
        finally:
            await asyncio.sleep(0)
            cleaned.add(device)

    backend.insert_wide_batch.side_effect = insert
    offsets = PartitionOffsets(partition=2, generation=15, next_commit_offset=0)
    task = asyncio.create_task(
        service.process(
            generation=15,
            offsets=offsets,
            records=[
                _record(offset, value=offset, device=f"wtg{offset:03d}") for offset in range(3)
            ],
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert started == {"wtg000", "wtg001", "wtg002"}
    assert cleaned == started
    assert offsets.next_commit_offset == 0
    metrics.record_wide_persisted.assert_not_called()
    metrics.record_unknown.assert_not_called()
    evidence.block_partition.assert_not_called()


@pytest.mark.asyncio
async def test_metrics_follow_all_group_success_and_offsets_confirm_contiguously() -> None:
    service, backend, _evidence, metrics = _service(writer_count=3)
    _vary_schema_by_device(service)
    all_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()
    completed: set[str] = set()
    metric_completions: list[frozenset[str]] = []
    metric_commit_offsets: list[int] = []

    async def insert(_schema: WideSchema, rows: Sequence[WideRecord]) -> None:
        device = rows[0].device_id
        started.add(device)
        if len(started) == 3:
            all_started.set()
        await release.wait()
        completed.add(device)

    backend.insert_wide_batch.side_effect = insert

    class ObservedOffsets(PartitionOffsets):
        def __init__(self) -> None:
            super().__init__(partition=2, generation=16, next_commit_offset=0)
            self.confirmations: list[tuple[int, int]] = []
            self.confirm_completions: list[frozenset[str]] = []

        def confirm(self, *, generation: int, offset: int) -> int:
            next_offset = super().confirm(generation=generation, offset=offset)
            self.confirmations.append((offset, next_offset))
            self.confirm_completions.append(frozenset(completed))
            return next_offset

    offsets = ObservedOffsets()

    def record_metrics(*, rows: int, points_per_row: int) -> None:
        assert (rows, points_per_row) == (1, 2)
        metric_completions.append(frozenset(completed))
        metric_commit_offsets.append(offsets.next_commit_offset)

    metrics.record_wide_persisted.side_effect = record_metrics
    task = asyncio.create_task(
        service.process(
            generation=16,
            offsets=offsets,
            records=[
                _record(offset, value=offset, device=f"wtg{offset:03d}") for offset in range(3)
            ],
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=2)
    metrics_before_success = metrics.record_wide_persisted.call_count
    commit_before_success = offsets.next_commit_offset
    release.set()
    result = await asyncio.wait_for(task, timeout=2)

    expected_groups = frozenset({"wtg000", "wtg001", "wtg002"})
    assert metrics_before_success == 0
    assert commit_before_success == 0
    assert metric_completions == [expected_groups] * 3
    assert offsets.confirm_completions == [expected_groups] * 3
    assert metric_commit_offsets == [3, 3, 3]
    assert offsets.confirmations == [(0, 1), (1, 2), (2, 3)]
    assert metrics.record_wide_persisted.call_count == 3
    assert result.next_commit_offset == 3
    assert result.accepted_records == 3
