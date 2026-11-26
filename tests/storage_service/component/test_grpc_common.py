"""Storage 真实 gRPC CommonService 与全局授权边界。"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, cast

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import storage_service_pb2 as storage_pb
from contracts import storage_service_pb2_grpc as storage_rpc
from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.infrastructure.adapters.inbound.grpc_server import (
    AuthenticationError,
    AuthorizationError,
    AuthorizationPolicy,
    build_storage_grpc_server,
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "storage-component-token-0123456789012345678901234567890123456789"
pytestmark = pytest.mark.component


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config() -> StorageConfig:
    path = ROOT / "configs/services/service_storage-main_v2.0.0.yaml"
    config = load_storage_config(path.read_bytes())
    return config.model_copy(
        update={"grpc": config.grpc.model_copy(update={"listen_port": _port()})}
    )


def _server(
    config: StorageConfig,
    *,
    config_status: str = "READY",
    revisions: tuple[str, str] | None = None,
) -> grpc.aio.Server:
    return build_storage_grpc_server(
        config=config,
        metadata=cast(Any, object()),
        wide_query=cast(Any, object()),
        wide_aggregation=cast(Any, object()),
        grouped_query=cast(Any, object()),
        jobs=cast(Any, object()),
        point_tables=cast(Any, object()),
        consumer=cast(Any, object()),
        cursor_secret=b"x" * 32,
        config_status=lambda: config_status,
        config_revisions=(lambda: revisions) if revisions is not None else None,
        metrics=StorageRuntimeMetrics(),
    )


async def test_common_service_uses_bearer_and_reports_only_real_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    server = _server(config)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as anonymous:
            await stub.GetServiceInfo(common.Empty())
        assert anonymous.value.code() == grpc.StatusCode.UNAUTHENTICATED

        info = await stub.GetServiceInfo(common.Empty(), metadata=metadata)
        assert info.service_id == "storage-main"
        assert info.process_role == "storage"
        status = await stub.GetServiceStatus(common.Empty(), metadata=metadata)
        assert status.health == "HEALTHY"
        capabilities = await stub.GetCapabilities(common.Empty(), metadata=metadata)
        assert "QuerySamples" in capabilities.rpc_names
        assert "QueryWideRows" not in capabilities.rpc_names
        assert "PushConfigSnapshot" not in capabilities.rpc_names

        with pytest.raises(grpc.aio.AioRpcError) as unsupported:
            await stub.GetMetrics(common.GetMetricsRequest(), metadata=metadata)
        assert unsupported.value.code() == grpc.StatusCode.UNIMPLEMENTED
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_every_storage_and_common_rpc_rejects_unauthorized_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enumerate the live proto, not a frozen count or hand-written method list."""
    from google.protobuf.message_factory import GetMessageClass

    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    server = _server(config)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    try:
        services = (
            (common.DESCRIPTOR, "CommonService", common_rpc.CommonServiceStub),
            (storage_pb.DESCRIPTOR, "StorageService", storage_rpc.StorageServiceStub),
        )
        observed: set[tuple[str, str]] = set()
        for file_descriptor, name, stub_cls in services:
            service = file_descriptor.services_by_name[name]
            stub = stub_cls(channel)
            for method in service.methods:
                assert not method.client_streaming and not method.server_streaming
                request = GetMessageClass(method.input_type)()
                for metadata in (
                    None,
                    (("authorization", "Bearer invalid-credential"),),
                ):
                    with pytest.raises(grpc.aio.AioRpcError) as rejected:
                        await getattr(stub, method.name)(request, metadata=metadata, timeout=5)
                    assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED, (
                        name,
                        method.name,
                    )
                observed.add((name, method.name))
        assert len(observed) == sum(
            len(descriptor.services_by_name[name].methods) for descriptor, name, _ in services
        )
    finally:
        await channel.close()
        await server.stop(grace=0)


class _Context:
    def __init__(self, token: str) -> None:
        self._metadata = (("authorization", f"Bearer {token}"),)

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


def test_global_retry_requires_admin_and_invalid_token_is_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    limited_caller = config.auth.callers[0].model_copy(update={"permissions": ["storage.operate"]})
    limited = config.model_copy(
        update={"auth": config.auth.model_copy(update={"callers": [limited_caller]})}
    )
    policy = AuthorizationPolicy(limited)
    with pytest.raises(AuthorizationError):
        policy.require(
            cast(Any, _Context(TOKEN)),
            permission="storage.operate",
            farm=None,
            global_scope=True,
        )
    with pytest.raises(AuthenticationError):
        policy.require(
            cast(Any, _Context("wrong-token-012345678901234567890123456789")),
            permission="storage.operate",
            farm=None,
            global_scope=True,
        )


async def test_global_retry_is_permission_denied_for_farm_scoped_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    limited_caller = config.auth.callers[0].model_copy(update={"permissions": ["storage.operate"]})
    limited = config.model_copy(
        update={"auth": config.auth.model_copy(update={"callers": [limited_caller]})}
    )
    server = _server(limited)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{limited.grpc.listen_port}")
    stub = storage_rpc.StorageServiceStub(channel)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await stub.RetryBlockedPartition(
                storage_pb.RetryBlockedPartitionRequest(request_id="retry-1"),
                metadata=(("authorization", f"Bearer {TOKEN}"),),
            )
        assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()
        await server.stop(grace=0)


class _RejectingConsumer:
    """Durable blocked evidence no longer matches: the retry must be refused, not crash."""

    async def retry_blocked_partition(
        self, *, request_id: str, topic: str, partition: int, expected_offset: int
    ) -> bool:
        return False


async def test_retry_blocked_partition_stale_offset_returns_failed_precondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    server = build_storage_grpc_server(
        config=config,
        metadata=cast(Any, object()),
        wide_query=cast(Any, object()),
        wide_aggregation=cast(Any, object()),
        grouped_query=cast(Any, object()),
        jobs=cast(Any, object()),
        point_tables=cast(Any, object()),
        consumer=_RejectingConsumer(),
        cursor_secret=b"x" * 32,
        config_status=lambda: "READY",
        metrics=StorageRuntimeMetrics(),
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = storage_rpc.StorageServiceStub(channel)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await stub.RetryBlockedPartition(
                storage_pb.RetryBlockedPartitionRequest(
                    request_id="retry-stale",
                    topic="windhub-history",
                    partition=0,
                    expected_blocked_offset=42,
                ),
                metadata=(("authorization", f"Bearer {TOKEN}"),),
            )
        assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_opt_in_anonymous_status_is_redacted_and_does_not_grant_read_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    base = _config()
    config = base.model_copy(
        update={"grpc": base.grpc.model_copy(update={"allow_unauthenticated_status": True})}
    )
    server = _server(
        config,
        config_status="CONFIG_HISTORY_UNAVAILABLE",
        revisions=("a" * 64, "b" * 64),
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    common_stub = common_rpc.CommonServiceStub(channel)
    storage_stub = storage_rpc.StorageServiceStub(channel)
    try:
        info = await common_stub.GetServiceInfo(common.Empty())
        assert info.process_role == "storage"
        assert info.contract_version == "windhub.storage.v2"
        status = await common_stub.GetServiceStatus(common.Empty())
        assert status.health == "DEGRADED"
        assert all(not dependency.detail for dependency in status.dependencies)
        assert not status.target_revision
        assert not status.active_revision
        authenticated_status = await common_stub.GetServiceStatus(
            common.Empty(),
            metadata=(("authorization", f"Bearer {TOKEN}"),),
        )
        assert authenticated_status.target_revision == "a" * 64
        assert authenticated_status.active_revision == "b" * 64
        capabilities = await common_stub.GetCapabilities(common.Empty())
        assert "QuerySamples" in capabilities.rpc_names
        assert "QueryWideRows" not in capabilities.rpc_names

        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await common_stub.GetServiceInfo(
                common.Empty(), metadata=(("authorization", "Bearer invalid"),)
            )
        assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED

        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await storage_stub.GetStorageStats(storage_pb.GetStorageStatsRequest())
        assert denied.value.code() == grpc.StatusCode.UNAUTHENTICATED

        with pytest.raises(grpc.aio.AioRpcError) as denied_query:
            await storage_stub.QuerySamples(storage_pb.QuerySamplesRequest())
        assert denied_query.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await channel.close()
        await server.stop(grace=0)
