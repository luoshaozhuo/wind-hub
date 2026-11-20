"""未知 PointTable 全量对账、不可变引用及并发消费边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.application.ingestion import (
    ConfigHistoryUnavailableError,
    IngestionBlockedError,
    IngestionService,
    KafkaRecord,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.domain.sample import StorageSample
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
    ImmutableConfigArchive,
    RepositoryDocument,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import StorageBundle
from storage_service.infrastructure.adapters.outbound.configuration_watch import (
    StorageConfigurationWatcher,
)

ROOT = Path(__file__).resolve().parents[3]
OLD = "point_table_turbine_v1.0.0"
NEW = "point_table_turbine_v2.0.0"
OTHER = "point_table_gearbox_v1.0.0"
pytestmark = pytest.mark.unit


def _config() -> StorageConfig:
    return load_storage_config((ROOT / "configs/services/service_storage_v1.0.1.yaml").read_bytes())


def _bundle(
    revision: int,
    tables: dict[str, tuple[str, str]],
    *,
    config: StorageConfig | None = None,
) -> StorageBundle:
    return StorageBundle(
        configuration=config or _config(),
        revision_id=revision,
        point_table_versions=tables,
        business_points_pin=None,
    )


def _document(table_id: str, version_id: str) -> RepositoryDocument:
    template_id = table_id.removeprefix("point_table_").split("_v", 1)[0]
    file_version = table_id.rsplit("_v", 1)[1]
    payload = json.dumps(
        {
            "schema_version": 1,
            "template_id": template_id,
            "file_version": file_version,
            "points": {"rotor_speed": {"unit": "m/s"}},
        }
    ).encode()
    return RepositoryDocument(
        config_type="point_table",
        config_id=template_id,
        version_id=version_id,
        revision_id=1,
        yaml_bytes=payload,
        yaml_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _sample(table_id: str) -> StorageSample:
    return StorageSample.from_kafka_json(
        json.dumps(
            {
                "schema_version": 2,
                "wind_farm_id": "farm-1",
                "collector_id": "collector-1",
                "task_id": "task-1",
                "batch_id": "batch-1",
                "device_type": "turbine",
                "device_id": "device-1",
                "point_table_id": table_id,
                "point_id": "rotor_speed",
                "business_point_id": "",
                "sample_id": f"sample-{table_id}",
                "data_level": "protocol",
                "quality": "good",
                "unit": "m/s",
                "value": 1.5,
                "sample_time": "2026-01-01T00:00:00Z",
                "publish_time": "2026-01-01T00:00:00Z",
            }
        ).encode()
    )


class _Fetcher:
    def __init__(self, documents: dict[str, RepositoryDocument]) -> None:
        self.documents = documents
        self.calls: list[tuple[str, str, str]] = []
        self.failure: Exception | None = None

    async def fetch_by_version(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> RepositoryDocument:
        self.calls.append((config_type, config_id, version_id))
        if self.failure is not None:
            raise self.failure
        return self.documents[version_id]


class _Metadata:
    def __init__(self) -> None:
        self.pins: dict[str, tuple[str, str]] = {}

    def register_semantic_pins(
        self,
        *,
        point_table_versions: dict[str, tuple[str, str]],
        business_points_pin: tuple[str, str] | None,
    ) -> None:
        assert business_points_pin is None
        for table_id, pin in point_table_versions.items():
            previous = self.pins.setdefault(table_id, pin)
            if previous != pin:
                raise ConfigHistoryUnavailableError("durable pin rebind")


class _Snapshot:
    def __init__(self) -> None:
        self.stored: list[StorageBundle] = []

    def store(self, bundle: StorageBundle) -> None:
        self.stored.append(bundle)


class _Client:
    def __init__(self, *bundles: StorageBundle) -> None:
        self.bundles = list(bundles)
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.failure: Exception | None = None

    async def fetch_service_bundle(self, *, service_id: str) -> StorageBundle:
        assert service_id == "storage-main"
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.failure is not None:
                raise self.failure
            return self.bundles.pop(0) if len(self.bundles) > 1 else self.bundles[0]
        finally:
            self.active -= 1


def _watcher(
    *,
    client: _Client,
    resolver: ConfigurationPointTableResolver,
    active: StorageBundle,
) -> tuple[StorageConfigurationWatcher, _Metadata, _Snapshot]:
    metadata = _Metadata()
    snapshot = _Snapshot()
    watcher = StorageConfigurationWatcher(
        client=cast(Any, client),
        resolver=resolver,
        bundle=active,
        service_id="storage-main",
        servers=["nats://127.0.0.1:4222"],
        metadata=cast(Any, metadata),
        snapshot=cast(Any, snapshot),
    )
    resolver.set_refresh_hook(watcher.refresh_now)
    return watcher, metadata, snapshot


async def test_unknown_point_table_reconciles_then_retries_original_identity(
    tmp_path: Path,
) -> None:
    documents = {
        pin[1]: _document(table_id, pin[1])
        for table_id, pin in ((OLD, ("turbine", "v1")), (NEW, ("turbine", "v2")))
    }
    fetcher = _Fetcher(documents)
    resolver = ConfigurationPointTableResolver(
        fetcher=fetcher,
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: ("turbine", "v1")},
    )
    refreshes = 0

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        resolver.add_immutable_pins(
            point_table_versions={OLD: ("turbine", "v1"), NEW: ("turbine", "v2")},
            business_points_pin=None,
        )

    resolver.set_refresh_hook(refresh)
    sample = _sample(NEW)
    await resolver.require_exact_versions([sample])

    assert refreshes == 1
    assert fetcher.calls == [("point_table", "turbine", "v2")]
    assert sample.point_table_id == NEW


async def test_configuration_failure_and_missing_target_fail_closed(tmp_path: Path) -> None:
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": _document(OLD, "v1")}),
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: ("turbine", "v1")},
    )

    async def unavailable() -> None:
        raise TimeoutError("Configuration unavailable")

    resolver.set_refresh_hook(unavailable)
    with pytest.raises(ConfigHistoryUnavailableError, match="refresh failed"):
        await resolver.require_exact_versions([_sample(NEW)])

    async def no_target() -> None:
        return None

    resolver.set_refresh_hook(no_target)
    with pytest.raises(ConfigHistoryUnavailableError, match="no pinned immutable version"):
        await resolver.require_exact_versions([_sample(NEW)])


async def test_concurrent_partitions_and_periodic_hint_share_one_pull(tmp_path: Path) -> None:
    pins = {OLD: ("turbine", "v1"), NEW: ("turbine", "v2"), OTHER: ("gearbox", "v3")}
    fetcher = _Fetcher({pin[1]: _document(table_id, pin[1]) for table_id, pin in pins.items()})
    resolver = ConfigurationPointTableResolver(
        fetcher=fetcher,
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: pins[OLD]},
    )
    client = _Client(_bundle(2, pins))
    client.release = asyncio.Event()
    watcher, metadata, snapshot = _watcher(
        client=client, resolver=resolver, active=_bundle(1, {OLD: pins[OLD]})
    )

    tasks = [
        asyncio.create_task(resolver.require_exact_versions([_sample(NEW)])),
        asyncio.create_task(resolver.require_exact_versions([_sample(OTHER)])),
        asyncio.create_task(watcher.refresh_now()),
    ]
    await asyncio.wait_for(client.entered.wait(), timeout=1)
    await asyncio.sleep(0)
    client.release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

    assert client.calls == 1
    assert client.max_active == 1
    assert metadata.pins == pins
    assert [item.revision_id for item in snapshot.stored] == [2]


async def test_same_name_versions_and_archive_fallback_remain_exact(tmp_path: Path) -> None:
    pins = {OLD: ("turbine", "v1"), NEW: ("turbine", "v2")}
    documents = {pin[1]: _document(table_id, pin[1]) for table_id, pin in pins.items()}
    archive = ImmutableConfigArchive(tmp_path)
    for document in documents.values():
        archive.persist(document)
    fetcher = _Fetcher(documents)
    fetcher.failure = ConnectionError("repository offline")
    resolver = ConfigurationPointTableResolver(
        fetcher=fetcher,
        archive=archive,
        point_table_versions=pins,
    )

    await resolver.require_exact_versions([_sample(OLD), _sample(NEW)])
    assert fetcher.calls == [("point_table", "turbine", "v1"), ("point_table", "turbine", "v2")]


async def test_yaml_identity_and_same_revision_mutation_are_rejected(tmp_path: Path) -> None:
    wrong = _document(OLD, "v1")
    wrong_payload = json.loads(wrong.yaml_bytes)
    wrong_payload["template_id"] = "different"
    wrong = RepositoryDocument(
        config_type=wrong.config_type,
        config_id=wrong.config_id,
        version_id=wrong.version_id,
        revision_id=wrong.revision_id,
        yaml_bytes=json.dumps(wrong_payload).encode(),
        yaml_sha256=hashlib.sha256(json.dumps(wrong_payload).encode()).hexdigest(),
    )
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": wrong}),
        archive=ImmutableConfigArchive(tmp_path / "resolver"),
        point_table_versions={OLD: ("turbine", "v1")},
    )
    with pytest.raises(ConfigHistoryUnavailableError, match="identity is inconsistent"):
        await resolver.require_exact_versions([_sample(OLD)])

    changed_config = _config().model_copy(
        update={"query": _config().query.model_copy(update={"max_page_size": 999})}
    )
    client = _Client(_bundle(1, {OLD: ("turbine", "v1")}, config=changed_config))
    watcher, metadata, snapshot = _watcher(
        client=client,
        resolver=resolver,
        active=_bundle(1, {OLD: ("turbine", "v1")}),
    )
    with pytest.raises(ConfigHistoryUnavailableError, match="changed an immutable revision"):
        await watcher.refresh_now()
    assert watcher.status == "CONFIG_HISTORY_UNAVAILABLE"
    assert metadata.pins == {}
    assert snapshot.stored == []


async def test_restart_required_keeps_verified_old_closure(tmp_path: Path) -> None:
    fetcher = _Fetcher({"v1": _document(OLD, "v1"), "v2": _document(NEW, "v2")})
    resolver = ConfigurationPointTableResolver(
        fetcher=fetcher,
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: ("turbine", "v1")},
    )
    changed = _config().model_copy(
        update={"query": _config().query.model_copy(update={"max_page_size": 999})}
    )
    client = _Client(
        _bundle(
            2,
            {OLD: ("turbine", "v1"), NEW: ("turbine", "v2")},
            config=changed,
        )
    )
    watcher, metadata, snapshot = _watcher(
        client=client, resolver=resolver, active=_bundle(1, {OLD: ("turbine", "v1")})
    )

    await watcher.refresh_now()
    assert watcher.status == "RESTART_REQUIRED"
    assert metadata.pins == {}
    assert snapshot.stored == []
    await resolver.require_exact_versions([_sample(OLD)])
    with pytest.raises(ConfigHistoryUnavailableError, match="no pinned immutable version"):
        await resolver.require_exact_versions([_sample(NEW)])


async def test_demand_failure_updates_health_without_poisoning_next_refresh(
    tmp_path: Path,
) -> None:
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": _document(OLD, "v1")}),
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: ("turbine", "v1")},
    )
    client = _Client(_bundle(1, {OLD: ("turbine", "v1")}))
    client.failure = TimeoutError("offline")
    watcher, _, _ = _watcher(
        client=client, resolver=resolver, active=_bundle(1, {OLD: ("turbine", "v1")})
    )
    with pytest.raises(TimeoutError, match="offline"):
        await watcher.refresh_now()
    assert watcher.status == "CONFIG_RECONCILE_DEGRADED"

    client.failure = None
    await watcher.refresh_now()
    assert watcher.status == "READY"


class _Backend:
    def __init__(self) -> None:
        self.rows: dict[str, frozenset[str]] = {}
        self.inserted: list[str] = []
        self.insert_started = asyncio.Event()
        self.insert_release: asyncio.Event | None = None

    async def ensure_tables(self, samples: Sequence[StorageSample]) -> None:
        return None

    async def fetch_content_hashes(
        self, samples: Sequence[StorageSample]
    ) -> dict[str, frozenset[str]]:
        return {
            sample.logical_key: self.rows[sample.logical_key]
            for sample in samples
            if sample.logical_key in self.rows
        }

    async def insert_batch(self, samples: Sequence[StorageSample]) -> None:
        self.insert_started.set()
        if self.insert_release is not None:
            await self.insert_release.wait()
        for sample in samples:
            self.rows[sample.logical_key] = frozenset((sample.content_sha256,))
            self.inserted.append(sample.point_table_id)


class _Evidence:
    def __init__(self) -> None:
        self.blocked: list[tuple[int, str, bytes]] = []

    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        self.blocked.append((offset, reason, payload))


def _record(sample: StorageSample, offset: int) -> KafkaRecord:
    payload = json.dumps(
        {
            "schema_version": 2,
            "wind_farm_id": sample.wind_farm_id,
            "collector_id": sample.collector_id,
            "task_id": sample.task_id,
            "batch_id": sample.batch_id,
            "device_type": sample.device_type,
            "device_id": sample.device_id,
            "point_table_id": sample.point_table_id,
            "point_id": sample.point_id,
            "business_point_id": "",
            "sample_id": sample.sample_id,
            "data_level": "protocol",
            "quality": "good",
            "unit": "m/s",
            "value": 1.5,
            "sample_time": "2026-01-01T00:00:00Z",
            "publish_time": "2026-01-01T00:00:00Z",
        }
    ).encode()
    return KafkaRecord(
        topic="samples",
        partition=0,
        offset=offset,
        key=b"farm-1:collector-1:device-1",
        value=payload,
    )


def _ingestion(
    resolver: ConfigurationPointTableResolver,
) -> tuple[IngestionService, _Backend, _Evidence]:
    backend = _Backend()
    evidence = _Evidence()
    return (
        IngestionService(
            storage=backend,
            point_tables=resolver,
            evidence=evidence,
            metrics=StorageRuntimeMetrics(),
            allowed_wind_farms=frozenset(("farm-1",)),
        ),
        backend,
        evidence,
    )


async def test_missing_target_blocks_and_retries_the_same_original_offset(tmp_path: Path) -> None:
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": _document(OLD, "v1"), "v2": _document(NEW, "v2")}),
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: ("turbine", "v1")},
    )

    async def no_target() -> None:
        return None

    resolver.set_refresh_hook(no_target)
    ingestion, backend, evidence = _ingestion(resolver)
    offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=7)
    sample = _sample(NEW)
    record = _record(sample, 7)
    with pytest.raises(IngestionBlockedError):
        await ingestion.process(generation=1, offsets=offsets, records=[record])
    assert offsets.next_commit_offset == 7
    assert backend.inserted == []
    assert evidence.blocked[0][0] == 7
    assert evidence.blocked[0][2] == record.value

    async def found() -> None:
        resolver.add_immutable_pins(
            point_table_versions={NEW: ("turbine", "v2")}, business_points_pin=None
        )

    resolver.set_refresh_hook(found)
    # KafkaStorageConsumer recreates the offset tracker when retrying a block.
    offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=7)
    result = await ingestion.process(generation=1, offsets=offsets, records=[record])
    assert result.next_commit_offset == 8
    assert backend.inserted == [NEW]


async def test_reference_only_update_can_overlap_an_inflight_write(tmp_path: Path) -> None:
    pins = {OLD: ("turbine", "v1"), NEW: ("turbine", "v2")}
    fetcher = _Fetcher({pin[1]: _document(table_id, pin[1]) for table_id, pin in pins.items()})
    resolver = ConfigurationPointTableResolver(
        fetcher=fetcher,
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: pins[OLD]},
    )
    client = _Client(_bundle(2, pins))
    watcher, _, snapshot = _watcher(
        client=client, resolver=resolver, active=_bundle(1, {OLD: pins[OLD]})
    )
    ingestion, backend, _ = _ingestion(resolver)
    backend.insert_release = asyncio.Event()
    offsets = PartitionOffsets(partition=0, generation=1, next_commit_offset=0)
    writing = asyncio.create_task(
        ingestion.process(generation=1, offsets=offsets, records=[_record(_sample(OLD), 0)])
    )
    await asyncio.wait_for(backend.insert_started.wait(), timeout=1)

    await watcher.refresh_now()
    backend.insert_release.set()
    await asyncio.wait_for(writing, timeout=1)
    await ingestion.process(generation=1, offsets=offsets, records=[_record(_sample(NEW), 1)])

    assert backend.inserted == [OLD, NEW]
    assert [item.revision_id for item in snapshot.stored] == [2]
