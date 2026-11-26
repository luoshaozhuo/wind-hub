"""PointTable、DeviceModel 与设备实例固定闭包的写入准入测试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_points
from storage_service.application.config import (
    DeviceBinding,
    DeviceModel,
    load_storage_config,
)
from storage_service.application.ingestion_contract import ConfigHistoryUnavailableError
from storage_service.domain.sample import DataLevel, StorageSample
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
    ImmutableConfigArchive,
    RepositoryDocument,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    StorageBundle,
    _parse_device_closure,
)
from storage_service.infrastructure.adapters.outbound.configuration_snapshot import (
    StorageBundleSnapshot,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import (
    MetadataConflictError,
    MetadataStore,
)

ROOT = Path(__file__).resolve().parents[3]
TABLE = "point_table_shared_v1.0.0"
NEW_TABLE = "point_table_shared_v2.0.0"
pytestmark = pytest.mark.unit


def _binding(
    table_id: str = TABLE,
    *,
    device_type: str = "turbine",
    farm: str = "farm-1",
    device: str = "device-1",
    model_version: str = "1.0.0",
    device_version: str = "1.0.0",
) -> DeviceBinding:
    suffix = f"{farm}-{device}-{table_id}-{model_version}-{device_version}"
    return DeviceBinding(
        wind_farm_id=farm,
        device_id=device,
        device_config_id="fleet",
        device_version_id=f"device-{suffix}",
        device_file_version=device_version,
        site_config_id="site",
        site_file_version="1.0.0",
        model=DeviceModel(
            config_id="model",
            version_id=f"model-{suffix}",
            file_version=model_version,
            device_type_id=device_type,
            point_table_id=table_id,
        ),
    )


def _table_document(table_id: str, device_type: str = "turbine") -> RepositoryDocument:
    config_id = table_id.removeprefix("point_table_").rsplit("_v", 1)[0]
    file_version = table_id.rsplit("_v", 1)[1]
    payload = yaml.safe_dump(
        {
            "schema_version": 1,
            "template_id": config_id,
            "file_version": file_version,
            "device_type": device_type,
            "points": {"rotor_speed": {"unit": "m/s"}},
        }
    ).encode()
    return RepositoryDocument(
        config_type="point_table",
        config_id=config_id,
        version_id=f"pin-{file_version}",
        revision_id=1,
        yaml_bytes=payload,
        yaml_sha256=hashlib.sha256(payload).hexdigest(),
    )


class _Fetcher:
    def __init__(self, documents: Sequence[RepositoryDocument]) -> None:
        self.documents = {document.version_id: document for document in documents}

    async def fetch_by_version(
        self, *, config_type: str, config_id: str, version_id: str
    ) -> RepositoryDocument:
        return self.documents[version_id]

    async def fetch_point_table_bundle(
        self, *, point_table_id: str, expected_manifest_sha256: str = ""
    ) -> tuple[RepositoryDocument, ...]:
        for document in self.documents.values():
            raw = yaml.safe_load(document.yaml_bytes)
            if f"point_table_{document.config_id}_v{raw['file_version']}" == point_table_id:
                return (document,)
        raise ConfigHistoryUnavailableError("historical PointTable not found")


def _resolver(
    directory: Path,
    tables: Mapping[str, tuple[str, str]],
    bindings: Sequence[DeviceBinding],
    *,
    device_type: str = "turbine",
) -> ConfigurationPointTableResolver:
    documents = [_table_document(table_id, device_type) for table_id in tables]
    return ConfigurationPointTableResolver(
        fetcher=_Fetcher(documents),
        archive=ImmutableConfigArchive(directory),
        point_table_versions=tables,
        device_bindings=bindings,
    )


def _sample(
    table_id: str = TABLE,
    *,
    device_type: str = "turbine",
    farm: str = "farm-1",
    device: str = "device-1",
) -> StorageSample:
    return StorageSample.from_kafka_json(
        json.dumps(
            {
                "schema_version": 2,
                "wind_farm_id": farm,
                "collector_id": "collector-1",
                "task_id": "task-1",
                "batch_id": "batch-1",
                "device_type": device_type,
                "device_id": device,
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


@pytest.mark.asyncio
async def test_pinned_protocol_unit_can_be_inferred_from_sink_wire(tmp_path: Path) -> None:
    """A missing unit is not a forged unit; the pinned table supplies authority."""
    now = datetime(2026, 11, 23, tzinfo=UTC)
    item = SampleItem(
        point_id="rotor_speed",
        sample_id="sink-protocol-source-unit",
        value=1.5,
        quality="good",
        sample_time=now,
    )
    batch = SampleBatch(
        batch_id="batch-sink-source-unit",
        mode="reliable",
        wind_farm_id="farm-1",
        collector_id="collector-1",
        task_id="task-1",
        device_type="turbine",
        device_id="device-1",
        point_table_id=TABLE,
        publish_time=now,
        samples=(item,),
    )
    payload = encode_kafka_points(batch, (item,), data_level="protocol")[0]
    sample = StorageSample.from_kafka_json(payload)
    assert sample.unit == ""
    resolver = _resolver(tmp_path, {TABLE: ("shared", "pin-1.0.0")}, [_binding()])
    await resolver.require_exact_versions((sample,))
    assert (
        await resolver.get_unit(
            point_table_id=TABLE,
            point_id="rotor_speed",
            business_point_id="",
            data_level=DataLevel.PROTOCOL,
        )
        == "m/s"
    )

    forged = json.loads(payload)
    forged["unit"] = "kW"
    mismatched = StorageSample.from_kafka_json(json.dumps(forged).encode())
    with pytest.raises(ConfigHistoryUnavailableError, match="PROTOCOL point unit"):
        await resolver.require_exact_versions((mismatched,))


@pytest.mark.asyncio
async def test_cold_restart_resolves_pinned_historical_point_unit(tmp_path: Path) -> None:
    """Query must be able to load an exact historical PointTable before ingest."""
    pins = {TABLE: ("shared", "pin-1.0.0")}
    prior = _resolver(tmp_path, pins, [_binding()])
    await prior.require_exact_versions((_sample(),))

    # Simulate Storage process restart: archive remains, but the in-memory
    # ConfigurationPointTableResolver document cache is empty.
    recovered = _resolver(tmp_path, pins, [_binding()])
    assert (
        await recovered.get_unit(
            point_table_id=TABLE,
            point_id="rotor_speed",
            business_point_id="",
            data_level=DataLevel.PROTOCOL,
        )
        == "m/s"
    )
    # The same immutable version must be available for subsequent ingestion.
    await recovered.require_exact_versions((_sample(),))


@pytest.mark.parametrize(
    ("device_type", "device_id"),
    [
        ("turbine", "WTG01"),
        ("photovoltaic", "PV01"),
        ("energy_storage", "BESS01"),
        ("grid", "GRID01"),
        ("plant_controller", "PPC01"),
    ],
)
async def test_all_device_categories_require_and_accept_authoritative_binding(
    tmp_path: Path, device_type: str, device_id: str
) -> None:
    binding = _binding(device_type=device_type, device=device_id)
    resolver = _resolver(
        tmp_path,
        {TABLE: ("shared", "pin-1.0.0")},
        [binding],
        device_type=device_type,
    )

    await resolver.require_exact_versions([_sample(device_type=device_type, device=device_id)])


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"device_type": "photovoltaic"}, "device_type disagrees"),
        ({"device": "unknown-device"}, "no fixed device instance"),
        ({"farm": "farm-2"}, "no fixed device instance"),
    ],
)
async def test_kafka_identity_cannot_override_device_closure(
    tmp_path: Path, override: dict[str, str], expected: str
) -> None:
    resolver = _resolver(tmp_path, {TABLE: ("shared", "pin-1.0.0")}, [_binding()])

    with pytest.raises(ConfigHistoryUnavailableError, match=expected):
        await resolver.require_exact_versions([_sample(**override)])


async def test_device_bound_to_old_model_and_table_cannot_use_new_version(
    tmp_path: Path,
) -> None:
    tables = {
        TABLE: ("shared", "pin-1.0.0"),
        NEW_TABLE: ("shared", "pin-2.0.0"),
    }
    resolver = _resolver(tmp_path, tables, [_binding(TABLE, model_version="1.0.0")])

    await resolver.require_exact_versions([_sample(TABLE)])
    with pytest.raises(ConfigHistoryUnavailableError, match="no fixed device instance"):
        await resolver.require_exact_versions([_sample(NEW_TABLE)])


def _repository_document(
    config_type: str,
    config_id: str,
    file_version: str,
    payload: dict[str, object],
) -> RepositoryDocument:
    content = yaml.safe_dump(payload).encode()
    return RepositoryDocument(
        config_type=config_type,
        config_id=config_id,
        version_id=f"{config_type}-{file_version}",
        revision_id=1,
        yaml_bytes=content,
        yaml_sha256=hashlib.sha256(content).hexdigest(),
    )


def _raw_closure(
    *,
    table_template: str = "shared",
    table_device_type: str = "turbine",
    model_table_version: str = "1.0.0",
    instance_model_version: str = "1.0.0",
    site_payload_id: str = "farm-1",
    site_document_version: str = "1.0.0",
) -> dict[tuple[str, str, str], RepositoryDocument]:
    table = _repository_document(
        "point_table",
        "shared",
        "1.0.0",
        {
            "schema_version": 1,
            "template_id": table_template,
            "file_version": "1.0.0",
            "device_type": table_device_type,
            "points": {},
        },
    )
    model = _repository_document(
        "device_model",
        "model",
        "1.0.0",
        {
            "schema_version": 1,
            "template_id": "model",
            "file_version": "1.0.0",
            "device_type_id": "turbine",
            "point_table": {"id": "shared", "version": model_table_version},
        },
    )
    device = _repository_document(
        "device",
        "fleet",
        "1.0.0",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "devices": {
                device_id: {"model": {"id": "model", "version": instance_model_version}}
                for device_id in ("WTG01", "WTG02")
            },
        },
    )
    site = _repository_document(
        "site",
        "site",
        "1.0.0",
        {
            "schema_version": 1,
            "file_version": site_document_version,
            "site_id": site_payload_id,
        },
    )
    return {
        ("point_table", "shared", "1.0.0"): table,
        ("device_model", "model", "1.0.0"): model,
        ("device", "fleet", "1.0.0"): device,
        ("site", "site", "1.0.0"): site,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"instance_model_version": "9.9.9"}, "device references absent fixed DeviceModel"),
        ({"model_table_version": "9.9.9"}, "pinned PointTable is absent"),
        ({"table_device_type": "photovoltaic"}, "device_type conflicts"),
        ({"table_template": "other"}, "PointTable document identity"),
        ({"site_document_version": "2.0.0"}, "selected Site identity"),
    ],
)
def test_bundle_rejects_incomplete_or_mismatched_device_closure(
    changes: dict[str, str], expected: str
) -> None:
    with pytest.raises(ConfigHistoryUnavailableError, match=expected):
        _parse_device_closure(_raw_closure(**changes), site_id="site", devices_id="fleet")


def test_deployment_missing_selected_instance_rejected() -> None:
    with pytest.raises(ConfigHistoryUnavailableError, match="missing or ambiguous"):
        _parse_device_closure(_raw_closure(), site_id="unknown", devices_id="fleet")


def test_bundle_retains_multiple_instances_from_one_versioned_device_document() -> None:
    points, bindings = _parse_device_closure(_raw_closure(), site_id="site", devices_id="fleet")

    assert points == {TABLE: ("shared", "point_table-1.0.0")}
    assert {binding.device_id for binding in bindings} == {"WTG01", "WTG02"}
    assert len(bindings) == 2


async def test_snapshot_restores_all_historical_device_bindings_without_main_changes(
    tmp_path: Path,
) -> None:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    tables = {
        TABLE: ("shared", "pin-1.0.0"),
        NEW_TABLE: ("shared", "pin-2.0.0"),
    }
    bindings = (
        _binding(TABLE, model_version="1.0.0"),
        _binding(NEW_TABLE, model_version="2.0.0", device_version="2.0.0"),
    )
    snapshot = StorageBundleSnapshot(tmp_path / "snapshot")
    snapshot.store(
        StorageBundle(
            configuration=config,
            revision_id=2,
            deployment_id="prod-a",
            target_revision="a" * 64,
            point_table_versions=tables,
            business_points_pin=None,
            device_bindings=bindings,
        )
    )

    restored = snapshot.load()
    assert restored.device_bindings == bindings
    resolver = ConfigurationPointTableResolver(
        fetcher=_Fetcher([_table_document(TABLE), _table_document(NEW_TABLE)]),
        archive=ImmutableConfigArchive(tmp_path / "archive"),
        point_table_versions=restored.point_table_versions,
    )
    await resolver.require_exact_versions([_sample(TABLE), _sample(NEW_TABLE)])


def test_sqlite_persists_historical_device_bindings_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage-meta.sqlite3"
    binding = _binding()
    pins = {TABLE: ("shared", "pin-1.0.0")}
    with MetadataStore(path) as metadata:
        metadata.register_semantic_pins(
            point_table_versions=pins,
            business_points_pin=None,
            device_bindings=(binding,),
        )
        assert metadata.list_device_bindings() == (binding,)

    with MetadataStore(path) as metadata:
        assert metadata.list_device_bindings() == (binding,)
        incompatible = replace(
            binding,
            model=replace(binding.model, device_type_id="photovoltaic"),
        )
        with pytest.raises(MetadataConflictError, match="cannot be rebound"):
            metadata.register_semantic_pins(
                point_table_versions=pins,
                business_points_pin=None,
                device_bindings=(incompatible,),
            )
        assert metadata.list_device_bindings() == (binding,)
        assert metadata.list_semantic_pins()[TABLE][0] == pins[TABLE]


def test_restart_target_is_durable_without_replacing_active_snapshot(tmp_path: Path) -> None:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    binding = _binding()
    active = StorageBundle(
        configuration=config,
        revision_id=10,
        deployment_id="prod-a",
        target_revision="a" * 64,
        point_table_versions={TABLE: ("shared", "pin-1.0.0")},
        business_points_pin=None,
        device_bindings=(binding,),
    )
    snapshot = StorageBundleSnapshot(tmp_path / "snapshot")
    snapshot.store(active)
    target = replace(active, revision_id=11, target_revision="b" * 64)
    snapshot.store_target(target)

    assert snapshot.load() == active
    assert snapshot.load_target() == target

    # A pending target never authorizes overwriting a verified active
    # generation with a stale authority revision.
    with pytest.raises(ConfigHistoryUnavailableError, match="older one"):
        snapshot.store_target(replace(target, revision_id=9))


def test_sqlite_schema_v1_upgrade_creates_historical_device_binding_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage-meta.sqlite3"
    with MetadataStore(path):
        pass
    # Model a pre-upgrade production SQLite database containing v1 metadata.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE device_binding_pins")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    with MetadataStore(path) as metadata:
        assert metadata.list_device_bindings() == ()
        metadata.register_semantic_pins(
            point_table_versions={TABLE: ("shared", "pin-1.0.0")},
            business_points_pin=None,
            device_bindings=(_binding(),),
        )
        assert len(metadata.list_device_bindings()) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
