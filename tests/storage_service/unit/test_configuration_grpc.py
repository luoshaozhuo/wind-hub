"""Storage authoritative Deployment-selected Configuration bundle tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

from contracts import configuration_repository_pb2 as pb
from core.configuration.deployment import closure_sha256
from storage_service.application.config import load_storage_config
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
        content_sha256=hashlib.sha256(
            json.dumps(
                content, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
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


async def test_historical_point_table_bundle_requires_exact_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONFIGURATION_TOKEN", "x" * 40)
    docs = [
        doc for doc in _files()
        if doc.config_id.config_type in {"point_table", "business_points", "units"}
    ]
    entries = [
        {
            "config_type": doc.config_id.config_type,
            "config_id": doc.config_id.config_id,
            "version_id": doc.version_id,
            "content_sha256": doc.content_sha256,
            "yaml_sha256": doc.yaml_sha256,
            "relative_path": doc.relative_path,
        }
        for doc in docs
    ]
    entries.sort(
        key=lambda item: (
            item["config_type"], item["config_id"], item["version_id"]
        )
    )
    digest = hashlib.sha256(
        json.dumps(
            entries, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode()
    ).hexdigest()

    class _PointStub:
        def __init__(self, *, corrupt: bool) -> None:
            self.corrupt = corrupt

        def __getattr__(self, name: str) -> Any:
            if name != "GetPointTableBundle":
                raise AttributeError(name)
            return self._get

        async def _get(self, request: object, **kwargs: object) -> object:
            assert isinstance(request, pb.GetPointTableBundleRequest)
            assert request.point_table_id == "point_table_shared_v1.0.0"
            return pb.GetPointTableBundleResponse(
                point_table_id=request.point_table_id,
                documents=docs,
                manifest_sha256="0" * 64 if self.corrupt else digest,
            )

    archive = _Archive()
    client = ConfigurationGrpcClient(
        endpoint="127.0.0.1:1",
        token_env="TEST_CONFIGURATION_TOKEN",
        archive=archive,
    )
    client._stub = cast(Any, _PointStub(corrupt=False))
    records = await client.fetch_point_table_bundle(
        point_table_id="point_table_shared_v1.0.0",
        expected_manifest_sha256=digest,
    )
    assert len(records) == 3
    assert len(archive.saved) == 3

    client._stub = cast(Any, _PointStub(corrupt=True))
    with pytest.raises(ConfigHistoryUnavailableError, match="manifest mismatch"):
        await client.fetch_point_table_bundle(
            point_table_id="point_table_shared_v1.0.0",
            expected_manifest_sha256=digest,
        )
    assert len(archive.saved) == 3


def test_service_v2_rejects_legacy_fixed_template_sources() -> None:
    document = yaml.safe_load(
        (ROOT / "configs/services/service_storage-main_v1.1.0.yaml").read_bytes()
    )
    document["config_sources"] = [
        {"config_type": "point_table", "config_id": "shared", "file_version": "1.0.0"}
    ]
    with pytest.raises(ValidationError):
        load_storage_config(yaml.safe_dump(document).encode())


def test_service_v2_requires_both_selected_instance_roles() -> None:
    document = yaml.safe_load(
        (ROOT / "configs/services/service_storage-main_v1.1.0.yaml").read_bytes()
    )
    del document["config_sources"]["instances"]["site"]
    with pytest.raises(ValidationError):
        load_storage_config(yaml.safe_dump(document).encode())


@pytest.mark.parametrize("corruption", ["semantic_hash", "duplicate_yaml_key", "yaml_alias"])
async def test_service_bundle_rejects_malformed_semantic_documents_before_archive(
    monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    monkeypatch.setenv("TEST_CONFIGURATION_TOKEN", "x" * 40)
    docs = _files()
    if corruption == "semantic_hash":
        docs[1].content_sha256 = "f" * 64
    elif corruption == "duplicate_yaml_key":
        docs[1].yaml_bytes += b"site_id: conflicting\n"
        docs[1].yaml_sha256 = hashlib.sha256(docs[1].yaml_bytes).hexdigest()
    else:
        docs[1].yaml_bytes += b"extra: &repeat same\ncopy: *repeat\n"
        docs[1].yaml_sha256 = hashlib.sha256(docs[1].yaml_bytes).hexdigest()
    archive = _Archive()
    client = ConfigurationGrpcClient(
        endpoint="127.0.0.1:1",
        token_env="TEST_CONFIGURATION_TOKEN",
        archive=archive,
    )
    client._stub = cast(Any, _Stub(_service_response(docs)))
    with pytest.raises(ConfigHistoryUnavailableError):
        await client.fetch_service_bundle(
            deployment_id="prod-a", service_id="storage-main"
        )
    assert archive.saved == []
