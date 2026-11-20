"""C2-T5: real TDengine 3.3.6.13 schema, idempotency and recovery checks.

Set WIND_HUB_TEST_TDENGINE_ENDPOINT=host:port plus the two credential variables.
The database is never replaced by a fake. Fault injection happens only after the
real DML has completed, then production readback/fencing code is exercised.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from storage_service.application.config import StorageConfig
from storage_service.application.ingestion import (
    IngestionBlockedError,
    IngestionService,
    KafkaRecord,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.application.query import HistoryFilter
from storage_service.domain.naming import get_object_names
from storage_service.domain.sample import DataLevel, StorageSample, ValueKind
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend
from storage_service.infrastructure.adapters.outbound.tdengine_schema import (
    SCHEMA_FINGERPRINT,
    expected_columns,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.fault_injection,
    pytest.mark.restart,
]

_BASE_US = 1_800_000_000_123_456


@dataclass
class _RealRuntime:
    config: StorageConfig
    metadata_path: Path
    metadata: MetadataStore
    backend: TDengineBackend
    raw: Any
    farm: str


class _ExactPointTables:
    async def require_exact_versions(self, samples: object) -> None:
        return None


def _payload(
    farm: str,
    *,
    device: str,
    point_table: str,
    point: str,
    sample_id: str = "sample-1",
    level: DataLevel = DataLevel.RAW,
    value: object = 42,
    quality: str = "good",
    timestamp_us: int = _BASE_US,
) -> bytes:
    business = "bp.active_power" if level == DataLevel.BUSINESS else ""
    return json.dumps(
        {
            "schema_version": 2,
            "wind_farm_id": farm,
            "collector_id": "collector-c2-t5",
            "task_id": "task-c2-t5",
            "batch_id": "batch-c2-t5",
            "device_type": "WTG",
            "device_id": device,
            "point_table_id": point_table,
            "point_id": point,
            "business_point_id": business,
            "sample_id": sample_id,
            "data_level": level.value,
            "quality": quality,
            "unit": "kW" if level == DataLevel.BUSINESS else "",
            "value": value,
            "sample_time": timestamp_us,
            "publish_time": timestamp_us + 10,
            "receive_time": timestamp_us + 20,
        },
        ensure_ascii=False,
    ).encode()


def _timestamp_payload(
    farm: str,
    *,
    device: str,
    point_table: str,
    point: str,
    sample_id: str = "sample-1",
    level: DataLevel = DataLevel.RAW,
    value: object = 42,
    quality: str = "good",
    timestamp_us: int = _BASE_US,
) -> bytes:
    """Build RFC3339 Kafka JSON without relying on a mocked decoder."""
    from datetime import UTC, datetime

    def rfc3339(value: int) -> str:
        moment = datetime.fromtimestamp(value / 1_000_000, UTC)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    data = json.loads(
        _payload(
            farm,
            device=device,
            point_table=point_table,
            point=point,
            sample_id=sample_id,
            level=level,
            value=value,
            quality=quality,
            timestamp_us=timestamp_us,
        )
    )
    data["sample_time"] = rfc3339(timestamp_us)
    data["publish_time"] = rfc3339(timestamp_us + 10)
    data["receive_time"] = rfc3339(timestamp_us + 20)
    return json.dumps(data, ensure_ascii=False).encode()


def _sample(runtime: _RealRuntime, **kwargs: Any) -> StorageSample:
    return StorageSample.from_kafka_json(_timestamp_payload(runtime.farm, **kwargs))


def _record(sample: StorageSample, *, offset: int, partition: int = 0) -> KafkaRecord:
    raw = _timestamp_payload(
        sample.wind_farm_id,
        device=sample.device_id,
        point_table=sample.point_table_id,
        point=sample.point_id,
        sample_id=sample.sample_id,
        level=sample.data_level,
        value=sample.value,
        quality=sample.quality.value,
        timestamp_us=sample.sample_time_us,
    )
    return KafkaRecord(
        topic="windhub-history",
        partition=partition,
        offset=offset,
        key=f"{sample.wind_farm_id}:{sample.collector_id}:{sample.device_id}".encode(),
        value=raw,
    )


def _ingestion(runtime: _RealRuntime, metrics: StorageRuntimeMetrics) -> IngestionService:
    return IngestionService(
        storage=runtime.backend,
        point_tables=_ExactPointTables(),
        evidence=runtime.metadata,
        metrics=metrics,
        allowed_wind_farms=frozenset((runtime.farm,)),
    )


def _filter(sample: StorageSample) -> HistoryFilter:
    return HistoryFilter(
        wind_farm_id=sample.wind_farm_id,
        data_level=sample.data_level,
        start_us=_BASE_US - 1,
        end_us=_BASE_US + 1_000_000,
        device_id=sample.device_id,
        point_table_id=sample.point_table_id,
    )


def _physical_rows(runtime: _RealRuntime, sample: StorageSample) -> list[tuple[object, ...]]:
    names = get_object_names(sample, SCHEMA_FINGERPRINT)
    return list(
        runtime.raw.query(
            f"SELECT ts, sample_key, logical_key, content_sha256, point_id "
            f"FROM {names.database}.{names.table} ORDER BY ts, sample_key"
        )
    )


@pytest.fixture
async def real_runtime(tmp_path: Path, request: pytest.FixtureRequest) -> Any:
    endpoint = os.environ.get("WIND_HUB_TEST_TDENGINE_ENDPOINT")
    if not endpoint:
        pytest.skip("real TDengine endpoint is not configured")
    taosws = pytest.importorskip("taosws")
    host, separator, port = endpoint.rpartition(":")
    if not separator:
        pytest.fail("TDengine endpoint must be host:port")
    user = os.environ.get("WIND_HUB_TDENGINE_USER", "root")
    password = os.environ.get("WIND_HUB_TEST_TDENGINE_PASSWORD")
    if password is None:
        pytest.skip("real TDengine credential is not configured")
    raw = taosws.connect(user=user, password=password, host=host, port=int(port))
    version = list(raw.query("SELECT SERVER_VERSION()"))[0][0]
    if version != "3.3.6.13":
        raw.close()
        pytest.fail(f"C2-T5 requires TDengine 3.3.6.13, got {version}")
    farm = "c2-t5-" + request.node.name.replace("test_", "")[:130] + uuid.uuid4().hex[:12]
    config = StorageConfig.model_validate(
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "service_id": "storage-c2-t5",
            "allowed_wind_farms": [farm],
            "kafka": {
                "bootstrap_servers": "127.0.0.1:9092",
                "topic": "windhub-history",
                "group_id": "storage-c2-t5",
            },
            "grpc": {},
            "auth": {
                "callers": [
                    {
                        "token_env": "WIND_HUB_TEST_STORAGE_TOKEN",
                        "permissions": ["storage.read"],
                        "wind_farms": [farm],
                    }
                ]
            },
            "tdengine": {
                "endpoint": endpoint,
                "credential_ref": "env:WIND_HUB_TEST_TDENGINE_PASSWORD",
                "database": {},
                "max_connections": 1,
                "max_inflight_sql": 1,
            },
            "configuration": {},
            "query": {},
            "export": {"root": str(tmp_path / "exports")},
            "backup": {"root": str(tmp_path / "backups")},
            "retry": {},
            "reconciliation": {"owner_instance_id": "storage-c2-t5"},
        }
    )
    metadata_path = tmp_path / "storage.sqlite3"
    metadata = MetadataStore(metadata_path)
    metadata.open()
    backend = TDengineBackend(config=config, metadata=metadata)
    await backend.open()
    try:
        yield _RealRuntime(config, metadata_path, metadata, backend, raw, farm)
    finally:
        await backend.close()
        metadata.close()
        raw.close()


async def test_g01_schema_tags_lengths_levels_and_six_scalar_types(
    real_runtime: _RealRuntime,
) -> None:
    runtime = real_runtime
    device = "WTG-schema"
    point_table = "pt-schema-v1"
    runtime.metadata.register_semantic_pins(
        point_table_versions={point_table: ("pt-schema-v1", "1.0.0")},
        business_points_pin=("bp-v1", "1.0.0"),
    )
    definitions = [
        (DataLevel.RAW, "i64", -(2**63), ValueKind.INT64, -(2**63)),
        (DataLevel.RAW, "u64", 2**64 - 1, ValueKind.UINT64, 2**64 - 1),
        (DataLevel.PROTOCOL, "f64", 1.23456789012345, ValueKind.DOUBLE, 1.23456789012345),
        (DataLevel.PROTOCOL, "bool", True, ValueKind.BOOL, True),
        (DataLevel.BUSINESS, "text", "有功功率-α", ValueKind.TEXT, "有功功率-α"),
        (DataLevel.BUSINESS, "bytes", b"\x00\xffpayload", ValueKind.BYTES, b"\x00\xffpayload"),
    ]
    samples: list[StorageSample] = []
    for index, (level, point, value, kind, expected) in enumerate(definitions):
        sample = _sample(
            runtime,
            device=device,
            point_table=point_table,
            point=point,
            level=level,
            value=None if kind == ValueKind.BYTES else value,
            timestamp_us=_BASE_US + index,
        )
        if kind == ValueKind.BYTES:
            content = hashlib.sha256(b"payload:" + bytes(value)).hexdigest()
            sample = replace(
                sample,
                value_kind=kind,
                value=expected,
                content_sha256=content,
                sample_key=hashlib.sha256(f"{sample.logical_key}:{content}".encode()).hexdigest(),
            )
        samples.append(sample)
    await runtime.backend.ensure_tables(samples)
    await runtime.backend.insert_batch(samples)

    for sample, (_, _, _, kind, expected) in zip(samples, definitions, strict=True):
        rows = await runtime.backend.fetch_history(
            replace(_filter(sample), point_id=sample.point_id), after=None, limit=20
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.value_kind == kind
        assert type(row.value) is type(expected)
        assert row.value == expected
        assert row.ts_us == sample.sample_time_us
        assert row.publish_time_us == sample.publish_time_us
        assert row.receive_time_us == sample.receive_time_us
        assert row.quality == "good"
        assert row.business_point_id == sample.business_point_id
        assert row.identity[4] == sample.data_level.value

        names = get_object_names(sample, SCHEMA_FINGERPRINT)
        described = list(runtime.raw.query(f"DESCRIBE {names.database}.{names.table}"))
        assert len(described) == len(expected_columns()) + 4
        for observed, (column, declaration) in zip(
            described[: len(expected_columns())], expected_columns(), strict=True
        ):
            assert observed[0].lower() == column
            expected_type = (
                declaration.split("(")[0].split(" ")[0].replace("UBIGINT", "BIGINT UNSIGNED")
            )
            assert observed[1].upper() == expected_type
            if "(" in declaration:
                assert observed[2] == int(declaration.split("(")[1].split(")")[0])
        assert "COMPOSITE KEY" in str(described[1][3]).upper()
        tags = {
            str(row[3]): str(row[5])
            for row in runtime.raw.query(f"SHOW TAGS FROM {names.database}.{names.table}")
        }
        assert tags == {
            "identity_hash": names.table_identity_sha256,
            "point_table_hash": hashlib.sha256(point_table.encode()).hexdigest(),
            "device_hash": hashlib.sha256(device.encode()).hexdigest(),
            "level_tag": str(
                {DataLevel.RAW: 1, DataLevel.PROTOCOL: 2, DataLevel.BUSINESS: 3}[sample.data_level]
            ),
        }
    ddl = list(runtime.raw.query(f"SHOW CREATE DATABASE {names.database}"))[0][-1]
    assert "PRECISION 'us'" in ddl


async def test_g02_composite_key_replay_conflict_and_same_timestamp_points(
    real_runtime: _RealRuntime,
) -> None:
    runtime = real_runtime
    point_table = "pt-idempotency-v1"
    runtime.metadata.register_semantic_pins(
        point_table_versions={point_table: (point_table, "1.0.0")},
        business_points_pin=None,
    )
    first = _sample(runtime, device="WTG-key", point_table=point_table, point="p1", value=10)
    second = _sample(
        runtime,
        device="WTG-key",
        point_table=point_table,
        point="p2",
        sample_id="sample-2",
        value=20,
    )
    assert first.sample_time_us == second.sample_time_us
    assert first.sample_key != second.sample_key
    conflict = _sample(runtime, device="WTG-key", point_table=point_table, point="p1", value=11)
    assert conflict.logical_key == first.logical_key
    assert conflict.content_sha256 != first.content_sha256
    assert conflict.sample_key != first.sample_key

    await runtime.backend.ensure_tables([first, second, conflict])
    await runtime.backend.insert_batch([first, second])
    await runtime.backend.insert_batch([first, second])
    assert len(_physical_rows(runtime, first)) == 2
    await runtime.backend.insert_batch([conflict])
    physical = _physical_rows(runtime, first)
    assert len(physical) == 3
    first_candidates = [row for row in physical if row[2] == first.logical_key]
    assert {row[3] for row in first_candidates} == {
        first.content_sha256,
        conflict.content_sha256,
    }
    hashes = await runtime.backend.fetch_content_hashes([first, conflict, second])
    assert hashes[first.logical_key] == frozenset((first.content_sha256, conflict.content_sha256))
    visible = await runtime.backend.fetch_history(_filter(first), after=None, limit=20)
    assert [row.point_id for row in visible] == ["p2"]
    assert visible[0].value == 20

    offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=30)
    with pytest.raises(IngestionBlockedError):
        await _ingestion(runtime, StorageRuntimeMetrics()).process(
            generation=1,
            offsets=offsets,
            records=[_record(first, offset=30)],
        )
    assert offsets.next_commit_offset == 30
    assert runtime.metadata.get_blocked_partition("windhub-history", 0) == (
        30,
        "SAMPLE_CONFLICT_PERSISTED",
    )


async def test_g02_real_write_then_lost_response_readback_and_replay(
    real_runtime: _RealRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = real_runtime
    point_table = "pt-response-loss-v1"
    runtime.metadata.register_semantic_pins(
        point_table_versions={point_table: (point_table, "1.0.0")},
        business_points_pin=None,
    )
    sample = _sample(runtime, device="WTG-unknown", point_table=point_table, point="p1")
    metrics = StorageRuntimeMetrics()
    ingestion = _ingestion(runtime, metrics)
    real_insert = runtime.backend.insert_batch
    calls = 0

    async def lose_response_after_real_write(samples: object) -> None:
        nonlocal calls
        calls += 1
        await real_insert(samples)  # type: ignore[arg-type]
        raise TimeoutError("injected client response loss after durable TDengine write")

    monkeypatch.setattr(runtime.backend, "insert_batch", lose_response_after_real_write)
    first_result = await ingestion.process(
        generation=3,
        offsets=PartitionOffsets(partition=0, generation=3, next_commit_offset=10),
        records=[_record(sample, offset=10)],
    )
    assert calls == 1
    assert first_result.next_commit_offset == 11
    assert first_result.accepted_records == 1
    assert metrics.snapshot().write_unknown == 1
    assert metrics.snapshot().persisted_points == 1
    assert len(_physical_rows(runtime, sample)) == 1

    replay = await ingestion.process(
        generation=3,
        offsets=PartitionOffsets(partition=0, generation=3, next_commit_offset=11),
        records=[_record(sample, offset=11)],
    )
    assert replay.next_commit_offset == 12
    assert replay.replayed_records == 1
    assert calls == 1
    assert metrics.snapshot().persisted_points == 1
    assert len(_physical_rows(runtime, sample)) == 1


async def test_g02_old_generation_response_cannot_commit(
    real_runtime: _RealRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = real_runtime
    point_table = "pt-generation-v1"
    runtime.metadata.register_semantic_pins(
        point_table_versions={point_table: (point_table, "1.0.0")},
        business_points_pin=None,
    )
    sample = _sample(runtime, device="WTG-generation", point_table=point_table, point="p1")
    ingestion = _ingestion(runtime, StorageRuntimeMetrics())
    old_offsets = PartitionOffsets(partition=0, generation=7, next_commit_offset=20)
    real_insert = runtime.backend.insert_batch

    async def revoke_after_real_write(samples: object) -> None:
        await real_insert(samples)  # type: ignore[arg-type]
        old_offsets.revoke(generation=7)

    monkeypatch.setattr(runtime.backend, "insert_batch", revoke_after_real_write)
    with pytest.raises(RuntimeError, match="generation is no longer owned"):
        await ingestion.process(
            generation=7,
            offsets=old_offsets,
            records=[_record(sample, offset=20)],
        )
    assert old_offsets.next_commit_offset == 20
    assert len(_physical_rows(runtime, sample)) == 1

    monkeypatch.undo()
    recovered = await ingestion.process(
        generation=8,
        offsets=PartitionOffsets(partition=0, generation=8, next_commit_offset=20),
        records=[_record(sample, offset=20)],
    )
    assert recovered.next_commit_offset == 21
    assert recovered.replayed_records == 1


async def test_sqlite_mapping_reopen_reverifies_real_schema_and_replay(
    real_runtime: _RealRuntime,
) -> None:
    runtime = real_runtime
    point_table = "pt-restart-v1"
    runtime.metadata.register_semantic_pins(
        point_table_versions={point_table: (point_table, "1.0.0")},
        business_points_pin=None,
    )
    sample = _sample(runtime, device="WTG-restart", point_table=point_table, point="p1")
    await runtime.backend.ensure_tables([sample])
    await runtime.backend.insert_batch([sample])
    before = runtime.metadata.list_tables()
    assert len(before) == 1 and before[0].state == "READY"
    await runtime.backend.close()
    runtime.metadata.close()

    reopened_metadata = MetadataStore(runtime.metadata_path)
    reopened_metadata.open()
    reopened_backend = TDengineBackend(config=runtime.config, metadata=reopened_metadata)
    await reopened_backend.open()
    try:
        assert reopened_metadata.list_semantic_pins()[point_table] == (
            (point_table, "1.0.0"),
            None,
        )
        assert reopened_metadata.list_tables()[0].identity_json == before[0].identity_json
        await reopened_backend.ensure_tables([sample])
        await reopened_backend.insert_batch([sample])
        assert len(_physical_rows(runtime, sample)) == 1
        rows = await reopened_backend.fetch_history(_filter(sample), after=None, limit=10)
        assert len(rows) == 1 and rows[0].value == sample.value
    finally:
        await reopened_backend.close()
        reopened_metadata.close()
