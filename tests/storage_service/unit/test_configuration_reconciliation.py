"""未知 PointTable 全量对账、不可变引用及并发消费边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from storage_service.application.config import (
    DeviceBinding,
    DeviceModel,
    StorageConfig,
    load_storage_config,
)
from storage_service.application.ingestion_contract import ConfigHistoryUnavailableError
from storage_service.domain.wide_record import WideDataLevel
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
    return load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )


def _bindings(tables: dict[str, tuple[str, str]]) -> tuple[DeviceBinding, ...]:
    return tuple(
        DeviceBinding(
            wind_farm_id="farm-1",
            device_id="device-1",
            device_config_id="devices",
            device_version_id=f"devices-{pin[1]}",
            device_file_version="1.0.0",
            site_config_id="site",
            site_file_version="1.0.0",
            model=DeviceModel(
                config_id="turbine-model",
                version_id=f"model-{pin[1]}",
                file_version=table_id.rsplit("_v", 1)[1],
                device_type_id="turbine",
                point_table_id=table_id,
            ),
        )
        for table_id, pin in tables.items()
    )


def _bundle(
    revision: int,
    tables: dict[str, tuple[str, str]],
    *,
    config: StorageConfig | None = None,
) -> StorageBundle:
    return StorageBundle(
        configuration=config or _config(),
        revision_id=revision,
        deployment_id="prod-a",
        target_revision=hashlib.sha256(
            json.dumps(
                [
                    config.model_dump(mode="json") if config else _config().model_dump(mode="json"),
                    sorted(tables.items()),
                ],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest(),
        point_table_versions=tables,
        business_points_pin=None,
        device_bindings=_bindings(tables),
    )


def _document(table_id: str, version_id: str) -> RepositoryDocument:
    template_id = table_id.removeprefix("point_table_").split("_v", 1)[0]
    file_version = table_id.rsplit("_v", 1)[1]
    payload = json.dumps(
        {
            "schema_version": 1,
            "template_id": template_id,
            "file_version": file_version,
            "device_type": "turbine",
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


async def _unit(
    resolver: ConfigurationPointTableResolver, table_id: str,
) -> str:
    return await resolver.get_wide_unit(
        point_table_id=table_id,
        point_id="rotor_speed",
        data_level=WideDataLevel.PROTOCOL,
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

    async def fetch_point_table_bundle(
        self, *, point_table_id: str, expected_manifest_sha256: str = ""
    ) -> tuple[RepositoryDocument, ...]:
        for document in self.documents.values():
            raw = yaml.safe_load(document.yaml_bytes)
            if f"point_table_{document.config_id}_v{raw['file_version']}" == point_table_id:
                self.calls.append((document.config_type, document.config_id, document.version_id))
                if self.failure is not None:
                    raise self.failure
                return (document,)
        raise ConfigHistoryUnavailableError("historical PointTable not found")


class _Metadata:
    def __init__(self) -> None:
        self.pins: dict[str, tuple[str, str]] = {}

    def register_semantic_pins(
        self,
        *,
        point_table_versions: dict[str, tuple[str, str]],
        business_points_pin: tuple[str, str] | None,
        device_bindings: tuple[DeviceBinding, ...] = (),
    ) -> None:
        assert business_points_pin is None
        assert isinstance(device_bindings, tuple)
        for table_id, pin in point_table_versions.items():
            previous = self.pins.setdefault(table_id, pin)
            if previous != pin:
                raise ConfigHistoryUnavailableError("durable pin rebind")


class _Snapshot:
    def __init__(self) -> None:
        self.stored: list[StorageBundle] = []
        self.pending: list[StorageBundle] = []

    def store(self, bundle: StorageBundle) -> None:
        self.stored.append(bundle)

    def store_target(self, bundle: StorageBundle) -> None:
        self.pending.append(bundle)


class _Client:
    def __init__(self, *bundles: StorageBundle) -> None:
        self.bundles = list(bundles)
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.failure: Exception | None = None

    async def get_current_revision(self) -> int:
        return self.bundles[0].revision_id

    async def fetch_service_bundle(self, *, deployment_id: str, service_id: str) -> StorageBundle:
        assert deployment_id == "prod-a"
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
        deployment_id="prod-a",
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
        device_bindings=_bindings({OLD: ("turbine", "v1")}),
    )
    refreshes = 0

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        resolver.add_immutable_pins(
            point_table_versions={OLD: ("turbine", "v1"), NEW: ("turbine", "v2")},
            business_points_pin=None,
            device_bindings=_bindings({OLD: ("turbine", "v1"), NEW: ("turbine", "v2")}),
        )

    resolver.set_refresh_hook(refresh)
    with pytest.raises(ConfigHistoryUnavailableError, match="immutable PointTable"):
        await _unit(resolver, NEW)
    assert refreshes == 0

    await refresh()
    assert await _unit(resolver, NEW) == "m/s"
    assert refreshes == 1
    assert fetcher.calls == [("point_table", "turbine", "v2")]


async def test_configuration_failure_and_missing_target_fail_closed(tmp_path: Path) -> None:
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": _document(OLD, "v1")}),
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: ("turbine", "v1")},
        device_bindings=_bindings({OLD: ("turbine", "v1")}),
    )

    async def unavailable() -> None:
        raise TimeoutError("Configuration unavailable")

    resolver.set_refresh_hook(unavailable)
    with pytest.raises(ConfigHistoryUnavailableError, match="immutable PointTable"):
        await _unit(resolver, NEW)

    async def no_target() -> None:
        return None

    resolver.set_refresh_hook(no_target)
    with pytest.raises(ConfigHistoryUnavailableError, match="immutable PointTable"):
        await _unit(resolver, NEW)


async def test_concurrent_partitions_and_periodic_hint_share_one_pull(tmp_path: Path) -> None:
    pins = {OLD: ("turbine", "v1"), NEW: ("turbine", "v2"), OTHER: ("gearbox", "v3")}
    fetcher = _Fetcher({pin[1]: _document(table_id, pin[1]) for table_id, pin in pins.items()})
    resolver = ConfigurationPointTableResolver(
        fetcher=fetcher,
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: pins[OLD]},
        device_bindings=_bindings({OLD: pins[OLD]}),
    )
    client = _Client(_bundle(2, pins))
    client.release = asyncio.Event()
    watcher, metadata, snapshot = _watcher(
        client=client, resolver=resolver, active=_bundle(1, {OLD: pins[OLD]})
    )

    tasks = [
        asyncio.create_task(watcher.refresh_now()),
        asyncio.create_task(watcher.refresh_now()),
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
        device_bindings=_bindings(pins),
    )

    assert await _unit(resolver, OLD) == "m/s"
    assert await _unit(resolver, NEW) == "m/s"
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
        device_bindings=_bindings({OLD: ("turbine", "v1")}),
    )
    with pytest.raises(ConfigHistoryUnavailableError, match="identity is inconsistent"):
        await _unit(resolver, OLD)

    changed_config = _config().model_copy(
        update={"query": _config().query.model_copy(update={"max_page_size": 999})}
    )
    client = _Client(_bundle(1, {OLD: ("turbine", "v1")}, config=changed_config))
    watcher, metadata, snapshot = _watcher(
        client=client,
        resolver=resolver,
        active=_bundle(1, {OLD: ("turbine", "v1")}),
    )
    with pytest.raises(
        ConfigHistoryUnavailableError, match="changed an immutable repository revision"
    ):
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
        device_bindings=_bindings({OLD: ("turbine", "v1")}),
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
    assert watcher.target_revision != watcher.active_revision
    assert [bundle.revision_id for bundle in snapshot.pending] == [2]
    assert metadata.pins == {}
    assert snapshot.stored == []
    assert await _unit(resolver, OLD) == "m/s"
    with pytest.raises(ConfigHistoryUnavailableError, match="immutable PointTable"):
        await _unit(resolver, NEW)


async def test_demand_failure_updates_health_without_poisoning_next_refresh(
    tmp_path: Path,
) -> None:
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": _document(OLD, "v1")}),
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions={OLD: ("turbine", "v1")},
        device_bindings=_bindings({OLD: ("turbine", "v1")}),
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


async def test_unrelated_repository_revision_does_not_rewrite_storage_snapshot(
    tmp_path: Path,
) -> None:
    pins = {OLD: ("turbine", "v1")}
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": _document(OLD, "v1")}),
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions=pins,
        device_bindings=_bindings(pins),
    )
    active = _bundle(7, pins)
    client = _Client(_bundle(8, pins))
    watcher, metadata, snapshot = _watcher(client=client, resolver=resolver, active=active)

    await watcher.refresh_now()

    assert watcher.status == "READY"
    assert watcher.target_revision == watcher.active_revision
    assert watcher.repository_revision_id == 8
    assert client.calls == 1
    assert metadata.pins == {}
    assert snapshot.stored == []


async def test_rejected_device_model_rebinding_is_not_persisted(
    tmp_path: Path,
) -> None:
    pins = {OLD: ("turbine", "v1")}
    active = _bundle(1, pins)
    original = active.device_bindings[0]
    conflicting = replace(
        original,
        model=replace(original.model, device_type_id="different"),
    )
    updated = StorageBundle(
        configuration=active.configuration,
        revision_id=2,
        deployment_id="prod-a",
        target_revision="b" * 64,
        point_table_versions=pins,
        business_points_pin=None,
        device_bindings=(conflicting,),
    )
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher({"v1": _document(OLD, "v1")}),
        archive=ImmutableConfigArchive(tmp_path),
        point_table_versions=pins,
        device_bindings=active.device_bindings,
    )
    watcher, metadata, snapshot = _watcher(
        client=_Client(updated), resolver=resolver, active=active
    )

    with pytest.raises(ConfigHistoryUnavailableError, match="DeviceModel version was rebound"):
        await watcher.refresh_now()

    assert watcher.status == "CONFIG_HISTORY_UNAVAILABLE"
    assert metadata.pins == {}
    assert snapshot.stored == []
