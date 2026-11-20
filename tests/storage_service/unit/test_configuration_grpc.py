"""Storage Configuration gRPC 多固定版本 bundle 解析。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from contracts import configuration_repository_pb2 as pb
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
)

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


def _document(
    config_type: str,
    config_id: str,
    version_id: str,
    file_version: str,
    yaml_bytes: bytes,
) -> pb.ConfigYamlDocument:
    return pb.ConfigYamlDocument(
        config_id=pb.ConfigId(config_type=config_type, config_id=config_id),
        version_id=version_id,
        file_version=file_version,
        revision_id=1,
        yaml_bytes=yaml_bytes,
        yaml_sha256=hashlib.sha256(yaml_bytes).hexdigest(),
        file_name=f"{config_type}/{config_id}_{file_version}.yaml",
    )


class _Archive:
    def persist(self, document: object) -> None:
        pass


class _Stub:
    def __init__(self, documents: list[pb.ConfigYamlDocument]) -> None:
        self._documents = documents

    async def ListConfigs(  # noqa: N802 — generated RPC method name
        self, request: object, **kwargs: object
    ) -> pb.ListConfigsResponse:
        return pb.ListConfigsResponse(
            items=[
                pb.ConfigSummary(
                    config_id=pb.ConfigId(config_type="service", config_id="storage-main"),
                    recommended_version_id="service-v1",
                )
            ],
            snapshot_revision_id=1,
        )

    async def GetConfigBundle(  # noqa: N802 — generated RPC method name
        self, request: object, **kwargs: object
    ) -> pb.GetConfigBundleResponse:
        digest = hashlib.sha256()
        for item in self._documents:
            digest.update(item.version_id.encode())
            digest.update(item.yaml_sha256.encode())
        return pb.GetConfigBundleResponse(
            revision_id=1,
            files=self._documents,
            manifest_sha256=digest.hexdigest(),
            total_bytes=sum(len(item.yaml_bytes) for item in self._documents),
        )


async def test_open_uses_generated_runtime_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CONFIGURATION_TOKEN", "x" * 40)
    client = ConfigurationGrpcClient(
        endpoint="127.0.0.1:1",
        token_env="TEST_CONFIGURATION_TOKEN",
        archive=_Archive(),
    )
    await client.open()
    assert client._client() is not None
    await client.close()


async def test_bundle_keeps_multiple_versions_of_same_config_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONFIGURATION_TOKEN", "x" * 40)
    service_payload = yaml.safe_load(
        (ROOT / "configs/services/service_storage_v1.0.1.yaml").read_bytes()
    )
    service_payload["config_sources"] = [
        {"config_type": "point_table", "config_id": "shared", "file_version": "1.0.0"},
        {"config_type": "point_table", "config_id": "shared", "file_version": "2.0.0"},
        {"config_type": "business_points", "config_id": "standard", "file_version": "1.0.0"},
        {"config_type": "units", "config_id": "standard", "file_version": "1.0.0"},
    ]
    documents = [
        _document(
            "service",
            "storage-main",
            "service-v1",
            "1.0.1",
            yaml.safe_dump(service_payload).encode(),
        ),
        _document("point_table", "shared", "point-v1", "1.0.0", b"point-v1"),
        _document("point_table", "shared", "point-v2", "2.0.0", b"point-v2"),
        _document("business_points", "standard", "business-v1", "1.0.0", b"business"),
        _document("units", "standard", "units-v1", "1.0.0", b"units"),
    ]
    client = ConfigurationGrpcClient(
        endpoint="127.0.0.1:1",
        token_env="TEST_CONFIGURATION_TOKEN",
        archive=_Archive(),
    )
    client._stub = _Stub(documents)
    bundle = await client.fetch_service_bundle(service_id="storage-main")
    assert bundle.point_table_versions == {
        "point_table_shared_v1.0.0": ("shared", "point-v1"),
        "point_table_shared_v2.0.0": ("shared", "point-v2"),
    }
    assert bundle.business_points_pin == ("standard", "business-v1")
