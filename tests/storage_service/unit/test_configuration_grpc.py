"""Storage authoritative Deployment-selected Configuration bundle tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from contracts import configuration_repository_pb2 as pb
from core.configuration.deployment import closure_sha256
from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
)

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


def _document(
    kind: str, config_id: str, version: str, content: dict[str, object]
) -> pb.ConfigYamlDocument:
    data = yaml.safe_dump(content).encode("utf-8")
    return pb.ConfigYamlDocument(
        config_id=pb.ConfigId(config_type=kind, config_id=config_id),
        version_id=f"{kind}-{config_id}-{version}",
        schema_version=int(content["schema_version"]),
        file_version=version,
        yaml_bytes=data,
        yaml_sha256=hashlib.sha256(data).hexdigest(),
        content_sha256=hashlib.sha256(data).hexdigest(),
        relative_path=f"{kind}/{config_id}_{version}.yaml",
    )


def _files() -> list[pb.ConfigYamlDocument]:
    service = yaml.safe_load(
        (ROOT / "configs/services/service_storage-main_v1.1.0.yaml").read_bytes()
    )
    return [
        _document("service", "storage-main", "1.1.0", service),
        _document(
            "site", "example_site", "1.0.0",
            {"schema_version": 1, "file_version": "1.0.0", "site_id": "farm-a"},
        ),
        _document(
            "device", "farm-a", "1.0.0",
            {
                "schema_version": 1, "file_version": "1.0.0",
                "devices": {"WTG01": {"model": {"id": "model", "version": "1.0.0"}}},
            },
        ),
        _document(
            "device_model", "model", "1.0.0",
            {
                "schema_version": 1, "file_version": "1.0.0",
                "template_id": "model", "device_type_id": "turbine",
                "point_table": {"id": "shared", "version": "1.0.0"},
            },
        ),
        _document(
            "point_table", "shared", "1.0.0",
            {
                "schema_version": 1, "file_version": "1.0.0",
                "template_id": "shared", "device_type": "turbine",
                "points": {"power": {"unit": "kW"}},
            },
        ),
        _document(
            "business_points", "standard", "1.0.0",
            {"schema_version": 1, "file_version": "1.0.0"},
        ),
        _document(
            "units", "standard", "1.0.0",
            {"schema_version": 1, "file_version": "1.0.0"},
        ),
    ]


def _service_response(
    documents: list[pb.ConfigYamlDocument], *, bad_target: bool = False
) -> pb.GetServiceConfigBundleResponse:
    manifest = hashlib.sha256()
    for doc in documents:
        for value in (doc.relative_path, doc.version_id, doc.yaml_sha256):
            encoded = value.encode("utf-8")
            manifest.update(len(encoded).to_bytes(4, "big"))
            manifest.update(encoded)
    closure = [
        (
            doc.config_id.config_type, doc.config_id.config_id,
            doc.file_version, doc.content_sha256,
        )
        for doc in documents
    ]
    target = closure_sha256("prod-a", "storage-main", closure)
    return pb.GetServiceConfigBundleResponse(
        repository_revision_id=10,
        target_revision="0" * 64 if bad_target else target,
        files=documents,
        manifest_sha256=manifest.hexdigest(),
        total_bytes=sum(len(doc.yaml_bytes) for doc in documents),
    )


class _Archive:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def persist(self, document: object) -> None:
        self.saved.append(document)


class _Stub:
    def __init__(self, response: pb.GetServiceConfigBundleResponse) -> None:
        self.response = response
        self.requests: list[object] = []

    def __getattr__(self, name: str) -> Any:
        if name == "GetServiceConfigBundle":
            return self._get_bundle
        if name == "GetCurrentRevision":
            return self._get_revision
        raise AttributeError(name)

    async def _get_bundle(self, request: object, **kwargs: object) -> object:
        self.requests.append(request)
        return self.response

    async def _get_revision(self, request: object, **kwargs: object) -> object:
        return pb.GetCurrentRevisionResponse(revision_id=10)


async def test_deployment_service_bundle_has_exact_selected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONFIGURATION_TOKEN", "x" * 40)
    archive = _Archive()
    client = ConfigurationGrpcClient(
        endpoint="127.0.0.1:1",
        token_env="TEST_CONFIGURATION_TOKEN",
        archive=archive,
    )
    client._stub = cast(Any, _Stub(_service_response(_files())))
    bundle = await client.fetch_service_bundle(
        deployment_id="prod-a", service_id="storage-main"
    )
    assert bundle.deployment_id == "prod-a"
    assert len(bundle.target_revision) == 64
    assert bundle.revision_id == 10
    assert bundle.point_table_versions == {
        "point_table_shared_v1.0.0": ("shared", "point_table-shared-1.0.0")
    }
    assert [item.device_id for item in bundle.device_bindings] == ["WTG01"]
    assert len(archive.saved) == 7
    assert await client.get_current_revision() == 10


async def test_service_bundle_rejects_wrong_closure_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONFIGURATION_TOKEN", "x" * 40)
    archive = _Archive()
    client = ConfigurationGrpcClient(
        endpoint="127.0.0.1:1",
        token_env="TEST_CONFIGURATION_TOKEN",
        archive=archive,
    )
    client._stub = cast(Any, _Stub(_service_response(_files(), bad_target=True)))
    with pytest.raises(ConfigHistoryUnavailableError, match="digest mismatch"):
        await client.fetch_service_bundle(
            deployment_id="prod-a", service_id="storage-main"
        )
    assert not archive.saved
