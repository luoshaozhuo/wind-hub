"""T-15: real Kafka/TDengine proof that JSON v1 records are strictly rejected.

The production ingestion chain (KafkaStorageConsumer + IngestionService +
TDengineBackend) runs against a real broker and a real TDengine 3.3.6.13
database. A valid v2 record is persisted and its offset committed; a legacy v1
record must then block the partition without auto-upgrade, silent mapping or
compatibility parsing, leave TDengine untouched and keep the Kafka cursor at
the last confirmed offset.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.structs import TopicPartition

from storage_service.application.config import StorageConfig
from storage_service.application.ingestion import IngestionService
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.domain.naming import get_object_names
from storage_service.domain.sample import StorageSample
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend
from storage_service.infrastructure.adapters.outbound.tdengine_schema import SCHEMA_FINGERPRINT
from tests.fixtures.services import r5_stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.kafka,
    pytest.mark.invalid_records,
]

_BASE_US = 1_800_000_000_000_000


class _ExactPointTables:
    async def require_exact_versions(self, samples: object) -> None:
        return None


def _rfc3339(microseconds: int) -> str:
    return (
        datetime.fromtimestamp(microseconds / 1_000_000, UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _v2_record(farm: str, *, schema_version: int, level: str = "raw") -> bytes:
    payload = {
        "schema_version": schema_version,
        "wind_farm_id": farm,
        "collector_id": "collector-f6",
        "task_id": "task-f6",
        "batch_id": "batch-f6",
        "device_type": "WTG",
        "device_id": "WTG-f6",
        "point_table_id": "pt-f6-v1",
        "point_id": "point.f6",
        "sample_id": "sample-f6-0001",
        "data_level": level,
        "quality": "good",
        "unit": "",
        "value": "002a" if level == "raw" else 42,
        "sample_time": _rfc3339(_BASE_US),
        "publish_time": _rfc3339(_BASE_US + 10),
        "receive_time": _rfc3339(_BASE_US + 20),
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


async def _wait(predicate: Any, description: str, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        outcome = predicate()
        last = await outcome if asyncio.iscoroutine(outcome) else outcome
        if last:
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


@pytest.mark.asyncio
async def test_legacy_json_v1_record_blocks_partition_without_commit_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_TDENGINE_USER", "root")
    monkeypatch.setenv("WIND_HUB_TEST_TDENGINE_PASSWORD", "taosdata")
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable")
    stack = await asyncio.to_thread(r5_stack.up_stack)
    token = uuid4().hex[:10]
    farm = f"f6-v1-{token}"
    topic = f"storage-v1-{token}"
    group = f"storage-v1-{token}"
    metadata = MetadataStore(tmp_path / "storage.sqlite3")
    metadata.open()
    consumer: KafkaStorageConsumer | None = None
    runner: asyncio.Task[None] | None = None
    admin: AIOKafkaAdminClient | None = None
    producer: AIOKafkaProducer | None = None
    try:
        admin = AIOKafkaAdminClient(bootstrap_servers=stack.kafka)
        await admin.start()
        await admin.create_topics([NewTopic(name=topic, num_partitions=1, replication_factor=1)])
        producer = AIOKafkaProducer(bootstrap_servers=stack.kafka)
        await producer.start()

        config = StorageConfig.model_validate(
            {
                "schema_version": 2,
                "file_version": "1.0.0",
                "service_id": "storage-f6-v1",
                "allowed_wind_farms": [farm],
                "config_sources": {"instances": {"site": "site-f6", "devices": "device-f6"}},
                "kafka": {
                    "bootstrap_servers": stack.kafka,
                    "topic": topic,
                    "group_id": group,
                    "max_partition_workers": 1,
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
                    "endpoint": stack.tdengine_rest,
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
                "reconciliation": {"owner_instance_id": "storage-f6-v1"},
            }
        )
        metadata.register_semantic_pins(
            point_table_versions={"pt-f6-v1": ("pt-f6-v1", "1.0.0")},
            business_points_pin=None,
        )
        backend = TDengineBackend(config=config, metadata=metadata)
        await backend.open()
        ingestion = IngestionService(
            storage=backend,
            point_tables=_ExactPointTables(),
            evidence=metadata,
            metrics=StorageRuntimeMetrics(),
            allowed_wind_farms=frozenset((farm,)),
        )
        consumer = KafkaStorageConsumer(
            configuration=config,
            bootstrap_servers=stack.kafka,
            ingestion=ingestion,
            metadata=metadata,
            metrics=StorageRuntimeMetrics(),
        )
        await consumer.start()
        runner = asyncio.create_task(consumer.run())

        tp = TopicPartition(topic, 0)

        message_key = f"{farm}:collector-f6:WTG-f6".encode()
        # Phase 1: a real v2 record is accepted, written and its offset committed.
        await producer.send_and_wait(topic, _v2_record(farm, schema_version=2), key=message_key)
        await _wait(
            lambda: _offset_is(admin, group, tp, 1),
            "v2 record commit at offset 1",
        )
        sample = StorageSample.from_kafka_json(_v2_record(farm, schema_version=2))
        names = get_object_names(sample, SCHEMA_FINGERPRINT)
        taosws = pytest.importorskip("taosws")
        host, _, port = stack.tdengine_rest.rpartition(":")
        raw = taosws.connect(user="root", password="taosdata", host=host, port=int(port))
        try:
            rows = list(raw.query(f"SELECT ts FROM {names.database}.{names.table} ORDER BY ts"))
            assert len(rows) == 1

            # Phase 2: a PROTOCOL record with the same source sample identity
            # is a distinct semantic row, not a collision with RAW bytes.
            await producer.send_and_wait(
                topic,
                _v2_record(farm, schema_version=2, level="protocol"),
                key=message_key,
            )
            await _wait(
                lambda: _offset_is(admin, group, tp, 2),
                "PROTOCOL v2 record commit at offset 2",
            )
            protocol = StorageSample.from_kafka_json(
                _v2_record(farm, schema_version=2, level="protocol")
            )
            protocol_names = get_object_names(protocol, SCHEMA_FINGERPRINT)
            assert protocol.logical_key != sample.logical_key
            assert (
                len(
                    list(
                        raw.query(
                            f"SELECT ts FROM {protocol_names.database}.{protocol_names.table}"
                        )
                    )
                )
                == 1
            )

            # Phase 3: a replay of identical RAW bytes is idempotent, while
            # the Kafka cursor advances after durable deduplication.
            await producer.send_and_wait(topic, _v2_record(farm, schema_version=2), key=message_key)
            await _wait(
                lambda: _offset_is(admin, group, tp, 3),
                "RAW idempotent replay commit at offset 3",
            )
            assert len(list(raw.query(f"SELECT ts FROM {names.database}.{names.table}"))) == 1

            # Phase 4: the legacy v1 record must be rejected with no compatibility path.
            await producer.send_and_wait(topic, _v2_record(farm, schema_version=1), key=message_key)
            await _wait(
                lambda: metadata.get_blocked_partition(topic, 0) is not None,
                "durable blocked partition evidence for the v1 record",
            )
            blocked_offset, reason = metadata.get_blocked_partition(topic, 0)
            assert blocked_offset == 3
            assert "schema_version" in reason
            blocked_page = metadata.list_blocked(page_size=100)
            assert (topic, 0, 3) == blocked_page[0][:3]

            # The cursor must stay at the last confirmed offset; give the broker
            # a real time window to prove no late commit advances past the blocker.
            await asyncio.sleep(3)
            offsets = await admin.list_consumer_group_offsets(group, partitions=[tp])
            assert offsets[tp].offset == 3

            # No second physical row may exist for the rejected record.
            rows = list(raw.query(f"SELECT ts FROM {names.database}.{names.table} ORDER BY ts"))
            assert len(rows) == 1
            list(raw.query(f"DROP DATABASE {names.database}"))
        finally:
            raw.close()
        await backend.close()
    finally:
        if consumer is not None:
            consumer.request_stop()
        if runner is not None:
            await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 20)
        if consumer is not None:
            await consumer.stop()
        if producer is not None:
            await producer.stop()
        if admin is not None:
            await admin.close()
        metadata.close()
        await asyncio.to_thread(r5_stack.down_stack, stack)


async def _offset_is(admin: AIOKafkaAdminClient, group: str, tp: Any, expected: int) -> bool:
    offsets = await admin.list_consumer_group_offsets(group, partitions=[tp])
    current = offsets.get(tp)
    return current is not None and current.offset == expected
