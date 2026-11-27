"""Repository 离线时仅从校验通过的本地闭包恢复 Storage。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import storage_service.main as storage_main
from storage_service.application.config import (
    DeviceBinding,
    DeviceModel,
    load_storage_config,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import StorageBundle
from storage_service.infrastructure.adapters.outbound.configuration_snapshot import (
    StorageBundleSnapshot,
)
from storage_service.settings import StorageBootstrap

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.component


class _OfflineConfiguration:
    def __init__(self) -> None:
        self.close_calls = 0

    async def open(self) -> None:
        return None

    async def fetch_service_bundle(self, *, deployment_id: str, service_id: str) -> StorageBundle:
        raise ConnectionError("Repository unavailable")

    async def close(self) -> None:
        self.close_calls += 1


class _Backend:
    instances: list[_Backend] = []

    def __init__(self, **kwargs: Any) -> None:
        self.close_calls = 0
        self.instances.append(self)

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1


class _Consumer:
    instances: list[_Consumer] = []

    def __init__(self, *, configuration: Any, **kwargs: Any) -> None:
        self.configuration = configuration
        self.recovering = False
        self.instances.append(self)

    async def start(self) -> None:
        return None

    async def run(self) -> None:
        return None

    def request_stop(self) -> None:
        return None


class _Server:
    async def start(self) -> None:
        return None

    async def stop(self, grace: int) -> None:
        return None


class _Jobs:
    def __init__(self, **kwargs: Any) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _Backup:
    def __init__(self, **kwargs: Any) -> None:
        return None


def _trusted_bundle() -> StorageBundle:
    configuration = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    point_table_id = "point_table_turbine_v1.0.0"
    binding = DeviceBinding(
        wind_farm_id="farm-1",
        device_id="device-1",
        device_config_id="devices",
        device_version_id="devices-1.0.0",
        device_file_version="1.0.0",
        site_config_id="site",
        site_file_version="1.0.0",
        model=DeviceModel(
            config_id="turbine-model",
            version_id="model-1.0.0",
            file_version="1.0.0",
            device_type_id="turbine",
            point_table_id=point_table_id,
        ),
    )
    return StorageBundle(
        configuration=configuration,
        revision_id=7,
        deployment_id="prod-a",
        target_revision="a" * 64,
        point_table_versions={point_table_id: ("turbine", "point-table-1.0.0")},
        business_points_pin=None,
        device_bindings=(binding,),
    )


async def test_repository_outage_restores_only_trusted_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _trusted_bundle()
    cache = tmp_path / "cache"
    snapshot = StorageBundleSnapshot(cache / "configuration-snapshots")
    snapshot.store(bundle)
    configuration = _OfflineConfiguration()
    jobs: list[_Jobs] = []
    status_callbacks: list[Any] = []

    monkeypatch.setattr(storage_main, "ConfigurationGrpcClient", lambda **kwargs: configuration)
    monkeypatch.setattr(storage_main, "TDengineBackend", _Backend)
    monkeypatch.setattr(storage_main, "KafkaStorageConsumer", _Consumer)
    monkeypatch.setattr(storage_main, "NativeTDengineBackup", _Backup)

    def jobs_factory(**kwargs: Any) -> _Jobs:
        job = _Jobs(**kwargs)
        jobs.append(job)
        return job

    def server_factory(**kwargs: Any) -> _Server:
        status_callbacks.append(kwargs["config_status"])
        return _Server()

    monkeypatch.setattr(storage_main, "AsyncStorageTasks", jobs_factory)
    monkeypatch.setattr(storage_main, "build_storage_grpc_server", server_factory)
    monkeypatch.setenv("WIND_HUB_STORAGE_CURSOR_SECRET", "x" * 32)
    bootstrap = StorageBootstrap.model_validate(
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "service_id": "storage-main",
            "deployment_id": "prod-a",
            "configuration": {
                "endpoint": "127.0.0.1:50051",
                "token_env": "WIND_HUB_ADMIN_TOKEN",
            },
            "nats": {"servers": ["nats://127.0.0.1:4222"]},
            "cache": {"directory": str(cache)},
        }
    )

    await storage_main.run_service(bootstrap)

    assert _Consumer.instances[-1].configuration == bundle.configuration
    assert status_callbacks[-1]() == "CONFIG_RECONCILE_DEGRADED"
    assert configuration.close_calls == 1
    assert _Backend.instances[-1].close_calls == 1
    assert jobs[-1].close_calls == 1
    assert snapshot.load() == bundle
