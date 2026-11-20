"""真实 IngestionService 配 fake TDengine 的 UNKNOWN 与幂等重放测试。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import pytest

from storage_service.application.ingestion import (
    IngestionService,
    KafkaRecord,
    WriteUnconfirmedError,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.domain.sample import StorageSample

pytestmark = pytest.mark.component


class _PointTables:
    async def require_exact_versions(self, samples: Sequence[StorageSample]) -> None:
        assert samples


class _Evidence:
    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        raise AssertionError(f"unexpected blocked record: {topic}:{partition}:{offset}:{reason}")


@dataclass
class _Storage:
    mode: Literal["before_write", "partial_unknown", "ack_lost"]
    rows: dict[str, str]
    insert_calls: int = 0

    async def ensure_tables(self, samples: Sequence[StorageSample]) -> None:
        assert samples

    async def fetch_content_hashes(
        self, samples: Sequence[StorageSample]
    ) -> Mapping[str, frozenset[str]]:
        return {
            sample.logical_key: (
                frozenset((self.rows[sample.logical_key],))
                if sample.logical_key in self.rows
                else frozenset()
            )
            for sample in samples
        }

    async def insert_batch(self, samples: Sequence[StorageSample]) -> None:
        self.insert_calls += 1
        if self.mode == "before_write":
            raise OSError("failed before any durable write")
        durable = samples[:1] if self.mode == "partial_unknown" else samples
        for sample in durable:
            self.rows[sample.logical_key] = sample.content_sha256
        raise OSError("write acknowledgement lost")


def _record(offset: int, sample_id: str) -> KafkaRecord:
    payload = {
        "schema_version": 2,
        "wind_farm_id": "farm",
        "collector_id": "collector",
        "task_id": "task",
        "batch_id": "batch",
        "device_type": "turbine",
        "device_id": "device",
        "point_table_id": "config:v1",
        "point_id": "power",
        "sample_id": sample_id,
        "data_level": "protocol",
        "quality": "good",
        "unit": "kW",
        "value": offset + 1,
        "sample_time": "2026-01-01T00:00:00Z",
        "publish_time": "2026-01-01T00:00:01Z",
    }
    return KafkaRecord(
        topic="windhub-history",
        partition=0,
        offset=offset,
        key=b"farm:collector:device",
        value=json.dumps(payload, separators=(",", ":")).encode(),
    )


def _service(storage: _Storage, metrics: StorageRuntimeMetrics) -> IngestionService:
    return IngestionService(
        storage=storage,
        point_tables=_PointTables(),
        evidence=_Evidence(),
        metrics=metrics,
        allowed_wind_farms=frozenset(("farm",)),
    )


async def test_failure_before_write_does_not_confirm_any_offset() -> None:
    storage = _Storage(mode="before_write", rows={})
    metrics = StorageRuntimeMetrics()
    offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=0)

    with pytest.raises(WriteUnconfirmedError):
        await _service(storage, metrics).process(
            generation=1,
            offsets=offsets,
            records=[_record(0, "a"), _record(1, "b")],
        )

    assert offsets.next_commit_offset == 0
    assert storage.rows == {}
    assert metrics.snapshot().persisted_points == 0
    assert metrics.snapshot().write_unknown == 1


async def test_partial_unknown_does_not_commit_confirmed_prefix() -> None:
    storage = _Storage(mode="partial_unknown", rows={})
    metrics = StorageRuntimeMetrics()
    offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=0)

    with pytest.raises(WriteUnconfirmedError):
        await _service(storage, metrics).process(
            generation=1,
            offsets=offsets,
            records=[_record(0, "a"), _record(1, "b")],
        )

    assert len(storage.rows) == 1
    assert offsets.next_commit_offset == 0
    assert metrics.snapshot().persisted_points == 0


async def test_lost_success_ack_recovers_and_replay_is_not_new_success() -> None:
    storage = _Storage(mode="ack_lost", rows={})
    metrics = StorageRuntimeMetrics()
    service = _service(storage, metrics)
    records = [_record(0, "a"), _record(1, "b")]
    first_offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=0)

    first = await service.process(generation=1, offsets=first_offsets, records=records)

    assert first.next_commit_offset == 2
    assert first.accepted_records == 2
    assert first.replayed_records == 0
    assert metrics.snapshot().persisted_points == 2
    assert metrics.snapshot().write_unknown == 1

    replay_offsets = PartitionOffsets(partition=0, generation=2, next_commit_offset=0)
    replay = await service.process(generation=2, offsets=replay_offsets, records=records)

    assert replay.next_commit_offset == 2
    assert replay.accepted_records == 2
    assert replay.replayed_records == 2
    assert storage.insert_calls == 1
    assert metrics.snapshot().persisted_points == 2
