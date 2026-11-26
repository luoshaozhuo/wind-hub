"""PointTable、DeviceModel 与设备实例固定闭包的写入准入测试。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from storage_service.application.config import (
    DeviceBinding,
    DeviceModel,
    load_storage_config,
)
from storage_service.application.ingestion_contract import ConfigHistoryUnavailableError
from storage_service.infrastructure.adapters.outbound.configuration_archive import RepositoryDocument
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
    assert restored.point_table_versions == tables
    assert {binding.device_file_version for binding in restored.device_bindings} == {
        "1.0.0", "2.0.0"
    }


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
