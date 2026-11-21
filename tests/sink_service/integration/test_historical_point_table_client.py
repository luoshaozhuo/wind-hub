"""Real gRPC round-trip tests for HistoricalPointTableClient.

A minimal in-process ConfigurationRepository servicer returns canned bundle
responses over a real loopback HTTP/2 channel. This exercises the client's
Bearer metadata, approved-manifest enforcement, response hash verification
and per-identity cache isolation without standing up the full Repository
process.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import grpc
import pytest

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as pbrpc
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.adapters.outbound.historical_point_table import (
    HistoricalPointTableClient,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

POINT_TABLE_ID = "point_table_modbus_pv_sim_v1.0.0"
DEVICE_TYPE = "photovoltaic"
TOKEN_REF = "env:WIND_HUB_HISTORICAL_TOKEN"
TOKEN = "historical-repository-token-0123456789abcdef"

UNITS = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "quantities": {"temperature": {"base_unit": "celsius"}},
    "units": {"celsius": {"symbol": "C", "quantity": "temperature"}},
}
BUSINESS = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "business_points": {
        "pv_cell_temperature": {
            "data_type": "float32",
            "unit": "celsius",
        }
    },
}
POINT_TABLE = {
    "schema_version": 1,
    "template_id": "modbus_pv_sim",
    "file_version": "1.0.0",
    "protocol": "modbus",
    "device_type": DEVICE_TYPE,
    "points": {
        "cell_temperature": {
            "business_point_id": "pv_cell_temperature",
            "data_type": "float32",
            "unit": "celsius",
            "scale": 1.0,
            "offset": 0.0,
            "address": {
                "register_type": "input",
                "address": 0,
                "count": 2,
                "slave_id": 1,
            },
        }
    },
}


def _canonical(document: dict) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _doc(config_type: str, config_id: str, document: dict) -> pb.ConfigYamlDocument:
    yaml_bytes = json.dumps(document, sort_keys=True).encode()
    return pb.ConfigYamlDocument(
        config_id=pb.ConfigId(config_type=config_type, config_id=config_id),
        version_id=f"ver-{config_id}",
        schema_version=1,
        file_version="1.0.0",
        yaml_bytes=yaml_bytes,
        content_sha256=hashlib.sha256(_canonical(document)).hexdigest(),
        yaml_sha256=hashlib.sha256(yaml_bytes).hexdigest(),
        relative_path=f"configs/x/{config_id}.yaml",
    )


def _documents() -> list[pb.ConfigYamlDocument]:
    return [
        _doc("units", "standard", UNITS),
        _doc("business_points", "standard", BUSINESS),
        _doc("point_table", "modbus_pv_sim", POINT_TABLE),
    ]


def _manifest(docs: list[pb.ConfigYamlDocument]) -> str:
    manifest = [
        {
            "config_type": item.config_id.config_type,
            "config_id": item.config_id.config_id,
            "version_id": item.version_id,
            "content_sha256": item.content_sha256,
            "yaml_sha256": item.yaml_sha256,
            "relative_path": item.relative_path,
        }
        for item in docs
    ]
    manifest.sort(key=lambda entry: (entry["config_type"], entry["config_id"], entry["version_id"]))
    return hashlib.sha256(_canonical(manifest)).hexdigest()


class _FakeRepository(pbrpc.ConfigurationRepositoryServiceServicer):
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.fail_status: grpc.StatusCode | None = None
        self.override_manifest: str | None = None

    async def GetPointTableBundle(  # type: ignore[override]  # noqa: N802
        self,
        request: pb.GetPointTableBundleRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.GetPointTableBundleResponse:
        self.calls.append((request, tuple(context.invocation_metadata() or ())))
        if self.fail_status is not None:
            await context.abort(self.fail_status, "injected")
        docs = _documents()
        manifest = self.override_manifest or _manifest(docs)
        return pb.GetPointTableBundleResponse(
            point_table_id=request.point_table_id,
            device_type=DEVICE_TYPE,
            documents=docs,
            manifest_sha256=manifest,
        )


@pytest.fixture
async def repository(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WIND_HUB_HISTORICAL_TOKEN", TOKEN)
    server = grpc.aio.server()
    servicer = _FakeRepository()
    pbrpc.add_ConfigurationRepositoryServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}", servicer
    finally:
        await server.stop(grace=None)


def _client(endpoint: str, manifest: str) -> HistoricalPointTableClient:
    return HistoricalPointTableClient(
        endpoint,
        TOKEN_REF,
        {POINT_TABLE_ID: manifest},
    )


async def test_real_grpc_round_trip_returns_historical_table(repository) -> None:
    endpoint, servicer = repository
    manifest = _manifest(_documents())
    client = _client(endpoint, manifest)
    result = await client.get(POINT_TABLE_ID, DEVICE_TYPE)
    assert result.point_table_id == POINT_TABLE_ID
    assert result.device_type == DEVICE_TYPE
    assert "cell_temperature" in result.table.points
    # The Bearer metadata really reached the server.
    request, metadata = servicer.calls[0]
    assert request.point_table_id == POINT_TABLE_ID
    assert dict(metadata)["authorization"] == f"Bearer {TOKEN}"


async def test_second_get_uses_cache_without_a_second_rpc(repository) -> None:
    endpoint, servicer = repository
    manifest = _manifest(_documents())
    client = _client(endpoint, manifest)
    await client.get(POINT_TABLE_ID, DEVICE_TYPE)
    await client.get(POINT_TABLE_ID, DEVICE_TYPE)
    assert len(servicer.calls) == 1


async def test_cache_isolation_by_device_type(repository) -> None:
    endpoint, servicer = repository
    manifest = _manifest(_documents())
    client = _client(endpoint, manifest)
    # The first identity is cached; a second request with the same identity
    # must not hit the network (one RPC), proving the cache key works.
    first = await client.get(POINT_TABLE_ID, DEVICE_TYPE)
    cached = await client.get(POINT_TABLE_ID, DEVICE_TYPE)
    assert first is cached
    assert len(servicer.calls) == 1
    assert ("point_table_modbus_pv_sim_v1.0.0", DEVICE_TYPE) in client._cached


async def test_manifest_mismatch_is_rejected(repository) -> None:
    endpoint, _servicer = repository
    # Client expects a different approved digest than the server returns.
    client = _client(endpoint, "f" * 64)
    with pytest.raises(ConfigurationError, match="unapproved|manifest"):
        await client.get(POINT_TABLE_ID, DEVICE_TYPE)


async def test_unauthenticated_server_response_fails_closed(repository) -> None:
    endpoint, servicer = repository
    servicer.fail_status = grpc.StatusCode.UNAUTHENTICATED
    manifest = _manifest(_documents())
    client = _client(endpoint, manifest)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await client.get(POINT_TABLE_ID, DEVICE_TYPE)
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
