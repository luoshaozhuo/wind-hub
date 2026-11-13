"""真实 mTLS 授权矩阵：CN → 权限集，服务端强制客户端证书。

自签 CA + 服务器证书 + 多 CN 客户端证书（write_mtls_pki），覆盖六类权限
（read/config_read/config_write/inject/lifecycle/diagnose）、未知 CN 拒止、
明文通道在 TLS 端口失败，以及非环口明文部署的配置期拒绝。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as pb_rpc
from contracts.scalar_codec import encode_scalar
from industrial_gateway.infrastructure.config.resolution import load_gateway
from tests.support.gateway_v1 import (
    FEEDBACK_UINT,
    reserve_port,
    start_gateway_v1,
    two_server_specs,
    write_config_tree,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service]

RIGHTS: dict[str, list[str]] = {
    "gw-reader": ["read"],
    "gw-injector": ["inject"],
    "gw-operator": ["read", "lifecycle"],
    "gw-diagnose": ["read", "diagnose"],
    "gw-admin": [
        "read",
        "config_read",
        "config_write",
        "inject",
        "lifecycle",
        "diagnose",
    ],
}
# gw-stranger：同 CA 签发但不在权限表中。
STRANGER = "gw-stranger"


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(
        tmp_path / "root",
        two_server_specs(control_enabled=False),
        mtls=RIGHTS,
        bootstrap_cn="gw-admin",
        extra_clients=[STRANGER],
    )
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_reader_can_read_but_not_inject_lifecycle_or_diagnose(harness) -> None:
    channel = harness.mtls_channel("gw-reader")
    try:
        common_stub = common_rpc.CommonServiceStub(channel)
        await common_stub.GetCapabilities(common.Empty())  # read 允许

        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await pb_rpc.IndustrialGatewayServiceStub(channel).UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id="gw1",
                    values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(1))],
                )
            )
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED

        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await pb_rpc.IndustrialGatewayServiceStub(channel).StopGateway(
                common.OperationRequest(id="gw1", expected_revision="x")
            )
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED

        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await common_stub.Diagnose(common.DiagnoseRequest(target_id="gw1"))
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


async def test_injector_updates_but_cannot_read(harness) -> None:
    channel = harness.mtls_channel("gw-injector")
    try:
        stub = pb_rpc.IndustrialGatewayServiceStub(channel)
        claim = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id="gw-injector",
                variable_ids=[FEEDBACK_UINT],
                action="ACQUIRE",
                lease_ms=10000,
            )
        )
        assert claim.accepted, claim.error_code
        reply = await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token=claim.lease_token,
                values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(42))],
            )
        )
        assert reply.results[0].success
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ListGateways(pb.ListGatewaysRequest())
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


async def test_diagnose_identity_executes_real_diagnosis(harness) -> None:
    denied = harness.mtls_channel("gw-reader")
    allowed = harness.mtls_channel("gw-diagnose")
    try:
        with pytest.raises(grpc.aio.AioRpcError):
            await common_rpc.CommonServiceStub(denied).Diagnose(
                common.DiagnoseRequest(target_id="gw1")
            )
        reply = await common_rpc.CommonServiceStub(allowed).Diagnose(
            common.DiagnoseRequest(target_id="gw1")
        )
        assert reply.overall_status
        assert reply.stages  # 真实执行注入/协议/控制分段
    finally:
        await denied.close()
        await allowed.close()


async def test_operator_lifecycle_but_no_config_write(harness) -> None:
    operator = harness.mtls_channel("gw-operator")
    admin = harness.mtls_channel("gw-admin")
    try:
        op_gw = pb_rpc.IndustrialGatewayServiceStub(operator)
        revision = (await op_gw.GetGateway(common.ObjectId(id="gw1"))).gateway.revision
        stopped = await op_gw.StopGateway(
            common.OperationRequest(id="gw1", expected_revision=revision)
        )
        assert stopped.accepted
        await op_gw.StartGateway(common.OperationRequest(id="gw1", expected_revision=revision))
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await common_rpc.CommonServiceStub(operator).PushConfigSnapshot(
                common.PushConfigSnapshotRequest(expected_revision=revision)
            )
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
        # admin 通过鉴权后由业务层返回校验失败（非 PERMISSION_DENIED）。
        reply = await common_rpc.CommonServiceStub(admin).PushConfigSnapshot(
            common.PushConfigSnapshotRequest(expected_revision=revision)
        )
        assert not reply.valid
    finally:
        await operator.close()
        await admin.close()


async def test_unknown_cn_denied_despite_valid_ca_signature(harness) -> None:
    channel = harness.mtls_channel(STRANGER)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await common_rpc.CommonServiceStub(channel).GetCapabilities(common.Empty())
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


async def test_plaintext_channel_rejected_on_tls_port(harness) -> None:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{harness.grpc_port}")
    try:
        with pytest.raises(grpc.aio.AioRpcError):
            await asyncio.wait_for(
                common_rpc.CommonServiceStub(channel).GetCapabilities(common.Empty()),
                timeout=3.0,
            )
    finally:
        await channel.close()


async def test_non_loopback_listener_without_tls_rejected_at_load(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    service_path = write_config_tree(
        root,
        two_server_specs(control_enabled=False),
        commander_endpoint="127.0.0.1:1",
        grpc_port=reserve_port(),
    )
    import yaml

    document = yaml.safe_load(service_path.read_text())
    document["grpc"]["listen_host"] = "0.0.0.0"
    service_path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError):
        load_gateway(service_path, project_root=root)
