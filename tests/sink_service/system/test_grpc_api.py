"""§14.3 test_grpc_api：正式 mTLS 接口可用性与 URI SAN 授权矩阵（真实进程）。

表 14-3 要求 CommonService 9 项 + SinkService 4 项在真实 gRPC Server/Stub、
mTLS 与授权拦截器下逐项可调用。本模块在真实三进程服务上验证认证/授权边界：

- 全权限调用方（spiffe://wind-hub/maintenance）query/control/config 均可达；
- query-only 调用方（spiffe://wind-hub/readonly）query 放行、control 与
  config 一律 PERMISSION_DENIED；
- 未登记证书（spiffe://wind-hub/outsider，CA 合法但不在 security.callers）
  任何 RPC 一律 PERMISSION_DENIED；
- 明文非 TLS 通道在握手阶段被拒，不进入任何业务方法。

被测拦截器为生产 ``PeerAuthorization``，证书由系统测试 TLS 束真实签发，
无 Mock、无测试专用服务端分支。
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
        "certificate_identity": "spiffe://wind-hub/maintenance",
        "permissions": ["query", "control", "config"],
    },
    {
        "caller_id": "readonly",
        "certificate_identity": "spiffe://wind-hub/readonly",
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


class TestMtlsAuthorizationMatrix:
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

    async def test_unregistered_certificate_denied_even_with_valid_ca(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """outsider：CA 合法但身份未登记，query 同样 PERMISSION_DENIED。"""
        sink_harness.write_dual_channel_root(reliable=False, callers=_CALLERS)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            async with sink_harness.channel(caller="outsider") as channel:
                assert await _status_code(channel, "query") == grpc.StatusCode.PERMISSION_DENIED
                assert await _status_code(channel, "control") == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await sink_harness.stop()

    async def test_plaintext_channel_is_rejected_before_any_handler(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """无客户端证书的明文通道在 TLS 握手期失败，业务方法不可达。"""
        sink_harness.write_dual_channel_root(reliable=False)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{sink_harness.grpc_port}") as channel:
                code = await _status_code(channel, "query")
                assert code in {
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.UNAUTHENTICATED,
                }
        finally:
            await sink_harness.stop()
