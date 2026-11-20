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
    path = ROOT / "configs/services/service_storage_v1.0.1.yaml"
    config = load_storage_config(path.read_bytes())
    return config.model_copy(
        update={"grpc": config.grpc.model_copy(update={"listen_port": _port()})}
    )


def _server(config: StorageConfig) -> grpc.aio.Server:
    return build_storage_grpc_server(
        config=config,
        metadata=cast(Any, object()),
        query=cast(Any, object()),
        jobs=cast(Any, object()),
        point_tables=cast(Any, object()),
        consumer=cast(Any, object()),
        cursor_secret=b"x" * 32,
        config_status=lambda: "READY",
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
        assert "PushConfigSnapshot" not in capabilities.rpc_names

        with pytest.raises(grpc.aio.AioRpcError) as unsupported:
            await stub.GetMetrics(common.GetMetricsRequest(), metadata=metadata)
        assert unsupported.value.code() == grpc.StatusCode.UNIMPLEMENTED
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
