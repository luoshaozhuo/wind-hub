"""Real Sink three-process gRPC authorization matrix with scoped Bearer callers.

A privileged caller may query, control and stage configuration. A query-only
caller must not control Deliveries or alter configuration. Unregistered or
missing Bearer tokens are denied by the production PeerAuthorization interceptor.
"""

from __future__ import annotations

import grpc
import pytest

from contracts import common_pb2, common_pb2_grpc, sink_service_pb2_grpc
from tests.sink_service.system.conftest import REALTIME_DELIVERY, SinkSystemHarness

pytestmark = [
    pytest.mark.system,
    pytest.mark.security,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.nats,
    pytest.mark.normal,
    pytest.mark.exception,
]

_CALLERS = [
    {
        "caller_id": "maintenance",
        "token_env": "env:WIND_HUB_SINK_CLI_TOKEN",
        "permissions": ["query", "control", "config"],
    },
    {
        "caller_id": "readonly",
        "token_env": "env:WIND_HUB_SINK_READONLY_TOKEN",
        "permissions": ["query"],
    },
]


async def _status_code(channel: grpc.aio.Channel, rpc: str) -> grpc.StatusCode | None:
    """调用指定 RPC，返回状态码；成功返回 None。"""
    common = common_pb2_grpc.CommonServiceStub(channel)
    sink = sink_service_pb2_grpc.SinkServiceStub(channel)
    try:
        if rpc == "query":
            await common.GetServiceStatus(common_pb2.Empty())
        elif rpc == "control":
            await sink.StartDelivery(common_pb2.OperationRequest(id=REALTIME_DELIVERY))
        elif rpc == "config":
            await common.PushConfigSnapshot(common_pb2.PushConfigSnapshotRequest())
        else:  # pragma: no cover - 测试自身防护
            raise AssertionError(f"unknown rpc probe {rpc}")
    except grpc.aio.AioRpcError as exc:
        return exc.code()
    return None


class TestBearerAuthorizationMatrix:
    async def test_full_permission_caller_reaches_all_rpc_classes(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """maintenance：query/control/config 三类 RPC 均不被授权层拒绝。"""
        sink_harness.write_dual_channel_root(reliable=False, callers=_CALLERS)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            async with sink_harness.channel(caller="admin") as channel:
                assert await _status_code(channel, "query") is None
                # control 可能因 revision 语义返回业务错误，但绝不允许是
                # PERMISSION_DENIED（授权层必须放行）。
                control_code = await _status_code(channel, "control")
                assert control_code != grpc.StatusCode.PERMISSION_DENIED
                config_code = await _status_code(channel, "config")
                assert config_code != grpc.StatusCode.PERMISSION_DENIED
        finally:
            await sink_harness.stop()

    async def test_query_only_caller_denied_control_and_config(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """readonly：query 放行，control/config 精确返回 PERMISSION_DENIED。"""
        sink_harness.write_dual_channel_root(reliable=False, callers=_CALLERS)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            async with sink_harness.channel(caller="readonly") as channel:
                assert await _status_code(channel, "query") is None
                assert await _status_code(channel, "control") == grpc.StatusCode.PERMISSION_DENIED
                assert await _status_code(channel, "config") == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await sink_harness.stop()

    async def test_unregistered_bearer_denied(self, sink_harness: SinkSystemHarness) -> None:
        """未注册 Bearer 身份无法访问任何 RPC。"""
        sink_harness.write_dual_channel_root(reliable=False, callers=_CALLERS)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            async with sink_harness.channel(caller="outsider") as channel:
                assert await _status_code(channel, "query") == grpc.StatusCode.PERMISSION_DENIED
                assert await _status_code(channel, "control") == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await sink_harness.stop()

    async def test_missing_bearer_is_rejected(self, sink_harness: SinkSystemHarness) -> None:
        """明文连接可达，但缺失 Bearer 必须得到 UNAUTHENTICATED。"""
        sink_harness.write_dual_channel_root(reliable=False)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{sink_harness.grpc_port}") as channel:
                code = await _status_code(channel, "query")
                assert code == grpc.StatusCode.UNAUTHENTICATED
        finally:
            await sink_harness.stop()
