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
        self.list_tokens: list[str] = []

    async def ListConfigs(  # noqa: N802 — generated RPC method name
        self, request: object, **kwargs: object
    ) -> pb.ListConfigsResponse:
        assert isinstance(request, pb.ListConfigsRequest)
        self.list_tokens.append(request.page_token)
        if not request.page_token:
            return pb.ListConfigsResponse(
                items=[
                    pb.ConfigSummary(
                        config_id=pb.ConfigId(config_type="service", config_id="other"),
                        recommended_version_id="other-v1",
                    )
                ],
                snapshot_revision_id=1,
                next_page_token="page-2",
            )
        assert request.page_token == "page-2"
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
        (ROOT / "configs/services/service_storage-main_v1.1.0.yaml").read_bytes()
    )
    service_payload["config_sources"] = [
        {"config_type": "point_table", "config_id": "shared", "file_version": "1.0.0"},
        {"config_type": "point_table", "config_id": "shared", "file_version": "2.0.0"},
        {"config_type": "business_points", "config_id": "standard", "file_version": "1.0.0"},
        {"config_type": "units", "config_id": "standard", "file_version": "1.0.0"},
        {"config_type": "device_model", "config_id": "model", "file_version": "1.0.0"},
        {"config_type": "device_model", "config_id": "model", "file_version": "2.0.0"},
        {
            "config_type": "device",
            "config_id": "fleet",
            "file_version": "1.0.0",
            "site_config_id": "site",
            "site_id": "farm-a",
            "site_file_version": "1.0.0",
        },
        {
            "config_type": "device",
            "config_id": "fleet",
            "file_version": "2.0.0",
            "site_config_id": "site",
            "site_id": "farm-a",
            "site_file_version": "1.0.0",
        },
        {"config_type": "site", "config_id": "site", "file_version": "1.0.0"},
    ]
    documents = [
        _document(
            "service",
            "storage-main",
            "service-v1",
            "1.0.1",
            yaml.safe_dump(service_payload).encode(),
        ),
        _document(
            "point_table",
            "shared",
            "point-v1",
            "1.0.0",
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "template_id": "shared",
                    "file_version": "1.0.0",
                    "device_type": "turbine",
                    "protocol": "modbus",
                    "points": {"power": {"unit": "kW"}},
                }
            ).encode(),
        ),
        _document(
            "point_table",
            "shared",
            "point-v2",
            "2.0.0",
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "template_id": "shared",
                    "file_version": "2.0.0",
                    "device_type": "turbine",
                    "protocol": "modbus",
                    "points": {"power": {"unit": "kW"}},
                }
            ).encode(),
        ),
        _document("business_points", "standard", "business-v1", "1.0.0", b"business"),
        _document("units", "standard", "units-v1", "1.0.0", b"units"),
        *[
            _document(
                "device_model",
                "model",
                f"model-v{version[0]}",
                version,
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "template_id": "model",
                        "file_version": version,
                        "device_type_id": "turbine",
                        "point_table": {"id": "shared", "version": version},
                    }
                ).encode(),
            )
            for version in ("1.0.0", "2.0.0")
        ],
        *[
            _document(
                "device",
                "fleet",
                f"device-v{version[0]}",
                version,
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "file_version": version,
                        "devices": {
                            "WTG01": {
                                "model": {"id": "model", "version": version},
                            }
                        },
                    }
                ).encode(),
            )
            for version in ("1.0.0", "2.0.0")
        ],
        _document(
            "site",
            "site",
            "site-v1",
            "1.0.0",
            yaml.safe_dump(
                {"schema_version": 1, "file_version": "1.0.0", "site_id": "farm-a"}
            ).encode(),
        ),
    ]
    client = ConfigurationGrpcClient(
        endpoint="127.0.0.1:1",
        token_env="TEST_CONFIGURATION_TOKEN",
        archive=_Archive(),
    )
    stub = _Stub(documents)
    client._stub = stub
    bundle = await client.fetch_service_bundle(service_id="storage-main")
    assert stub.list_tokens == ["", "page-2"]
    assert bundle.point_table_versions == {
        "point_table_shared_v1.0.0": ("shared", "point-v1"),
        "point_table_shared_v2.0.0": ("shared", "point-v2"),
    }
    assert bundle.business_points_pin == ("standard", "business-v1")
    assert [(item.device_version_id, item.model.version_id) for item in bundle.device_bindings] == [
        ("device-v1", "model-v1"),
        ("device-v2", "model-v2"),
    ]
    assert {item.point_table_id for item in bundle.device_bindings} == set(
        bundle.point_table_versions
    )
    assert {item.wind_farm_id for item in bundle.device_bindings} == {"farm-a"}
    assert {item.site_config_id for item in bundle.device_bindings} == {"site"}
