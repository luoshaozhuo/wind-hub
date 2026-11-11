"""R1 P1-03：真实 mTLS 双 CN 的租约认证主体隔离。

两个由同一测试 CA 真实签发的客户端身份（gw-injector-a / gw-injector-b）都持有
``inject`` RPC 权限，证明"有 RPC 权限但不是租约认证主体"时服务端仍然拒止。
服务端是生产 ``GatewayGrpcService`` + ``GatewayRuntime``（start_gateway_v1
不替换任何生产对象），认证主体取自证书 CN，客户端自报 owner_id 只是业务标识。

覆盖任务书 P1-03 的 1/2/3/4/5/6/7/8/9 项；进程重启失效由
tests/industrial_gateway/system/test_claims_restart.py 真实子进程用例覆盖，
Data Generator owner_id 语义由 data_generator 回归套件覆盖。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest

from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as pb_rpc
from contracts.scalar_codec import decode_scalar, encode_scalar
from tests.support.gateway_v1 import (
    FEEDBACK_UINT,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service, pytest.mark.safety]

PRINCIPAL_A = "gw-injector-a"
PRINCIPAL_B = "gw-injector-b"
OWNER_A = "generation-owner-a"

RIGHTS: dict[str, list[str]] = {
    PRINCIPAL_A: ["inject", "read"],
    PRINCIPAL_B: ["inject", "read"],
    # 同 CA 签发但无 inject 权限，用于 PERMISSION_DENIED 对照。
    "gw-reader": ["read"],
    # 引导通道需要全部权限以做独立读数核对。
    "gw-admin": ["read", "inject", "lifecycle", "diagnose", "config_read", "config_write"],
}


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(
        tmp_path / "root",
        two_server_specs(control_enabled=False),
        mtls=RIGHTS,
        bootstrap_cn="gw-admin",
    )
    try:
        yield gateway
    finally:
        await gateway.stop()


def _stub(harness, cn: str) -> pb_rpc.IndustrialGatewayServiceStub:
    return pb_rpc.IndustrialGatewayServiceStub(harness.mtls_channel(cn))


async def _read_admin_value(harness) -> int | None:
    reply = await harness.gateway_stub().ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_UINT])
    )
    sample = reply.values[0]
    if sample.quality == "UNINITIALIZED":
        return None
    return decode_scalar(sample.value)


async def test_a_full_lifecycle_succeeds_and_b_is_denied_every_step(harness) -> None:
    """任务书 1-6：A 全链路成功；B 持相同 owner/集合/token 的 RENEW/RELEASE/update 全拒。"""
    channel_a = harness.mtls_channel(PRINCIPAL_A)
    channel_b = harness.mtls_channel(PRINCIPAL_B)
    try:
        stub_a = pb_rpc.IndustrialGatewayServiceStub(channel_a)
        stub_b = pb_rpc.IndustrialGatewayServiceStub(channel_b)

        acquired = await stub_a.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="ACQUIRE",
                lease_ms=30000,
            )
        )
        assert acquired.accepted, acquired.error_code
        token = acquired.lease_token

        # B 复用 A 的 owner_id、变量集合与 token：RENEW 与 RELEASE 都必须被拒。
        renew_b = await stub_b.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="RENEW",
                lease_token=token,
                lease_ms=30000,
            )
        )
        assert not renew_b.accepted and renew_b.error_code == "LEASE_LOST"

        release_b = await stub_b.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="RELEASE",
                lease_token=token,
            )
        )
        assert not release_b.accepted and release_b.error_code == "LEASE_LOST"

        # B 用 A 的 token 调 UpdateVariables：FAILED_PRECONDITION，且不得产生写入。
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub_b.UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id="gw1",
                    lease_token=token,
                    values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(99))],
                )
            )
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        assert await _read_admin_value(harness) is None

        # B 的全部拒止没有破坏 A 的租约：A 自己 RENEW、update、RELEASE 全部成功。
        renew_a = await stub_a.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="RENEW",
                lease_token=token,
                lease_ms=30000,
            )
        )
        assert renew_a.accepted and renew_a.lease_token == token

        update_a = await stub_a.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token=token,
                values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(42))],
            )
        )
        assert update_a.results[0].success
        assert await _read_admin_value(harness) == 42

        release_a = await stub_a.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="RELEASE",
                lease_token=token,
            )
        )
        assert release_a.accepted
    finally:
        await channel_a.close()
        await channel_b.close()


async def test_caller_without_inject_permission_gets_permission_denied(harness) -> None:
    """任务书 7：缺少 inject 权限的主体在 RPC 边界即 PERMISSION_DENIED。"""
    channel = harness.mtls_channel("gw-reader")
    try:
        stub = pb_rpc.IndustrialGatewayServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ClaimTargets(
                pb.ClaimTargetsRequest(
                    gateway_id="gw1",
                    owner_id="reader-attempt",
                    variable_ids=[FEEDBACK_UINT],
                    action="ACQUIRE",
                    lease_ms=10000,
                )
            )
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id="gw1",
                    lease_token="whatever",
                    values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(1))],
                )
            )
        assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


async def test_expired_token_is_fenced_even_for_its_own_principal(harness) -> None:
    """任务书 8（过期 token）：租约到期后原主体也不能再 RENEW/update。"""
    channel = harness.mtls_channel(PRINCIPAL_A)
    try:
        stub = pb_rpc.IndustrialGatewayServiceStub(channel)
        acquired = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="ACQUIRE",
                lease_ms=1000,
            )
        )
        assert acquired.accepted
        token = acquired.lease_token
        await asyncio.sleep(1.1)

        renew = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="RENEW",
                lease_token=token,
                lease_ms=1000,
            )
        )
        assert not renew.accepted and renew.error_code == "LEASE_LOST"
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id="gw1",
                    lease_token=token,
                    values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(7))],
                )
            )
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        assert await _read_admin_value(harness) is None
    finally:
        await channel.close()


async def test_config_apply_invalidation_fences_existing_token(harness) -> None:
    """任务书 8（配置 Apply）：生产 apply 路径调用的 invalidate() 必须使旧 token 失效。"""
    channel = harness.mtls_channel(PRINCIPAL_A)
    try:
        stub = pb_rpc.IndustrialGatewayServiceStub(channel)
        acquired = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=OWNER_A,
                variable_ids=[FEEDBACK_UINT],
                action="ACQUIRE",
                lease_ms=30000,
            )
        )
        assert acquired.accepted
        # 与 GatewayRuntime.apply_configuration 中服务器变更时调用的是同一生产方法。
        harness.runtime.variable_leases.invalidate("gw1")
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id="gw1",
                    lease_token=acquired.lease_token,
                    values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(5))],
                )
            )
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()


async def test_concurrent_acquire_and_cross_principal_update_race(harness) -> None:
    """任务书 9：并发 ACQUIRE 只有一个主体胜出；混发 update 不发生跨主体写入。"""
    channel_a = harness.mtls_channel(PRINCIPAL_A)
    channel_b = harness.mtls_channel(PRINCIPAL_B)
    try:
        stub_a = pb_rpc.IndustrialGatewayServiceStub(channel_a)
        stub_b = pb_rpc.IndustrialGatewayServiceStub(channel_b)

        async def acquire(stub, owner: str) -> pb.ClaimTargetsResponse:
            return await stub.ClaimTargets(
                pb.ClaimTargetsRequest(
                    gateway_id="gw1",
                    owner_id=owner,
                    variable_ids=[FEEDBACK_UINT],
                    action="ACQUIRE",
                    lease_ms=30000,
                )
            )

        results = await asyncio.gather(
            acquire(stub_a, OWNER_A),
            acquire(stub_b, "generation-owner-b"),
        )
        accepted = [r for r in results if r.accepted]
        rejected = [r for r in results if not r.accepted]
        assert len(accepted) == 1 and len(rejected) == 1
        assert rejected[0].error_code == "TARGET_CLAIMED"
        winner_token = accepted[0].lease_token

        # A、B 同时用胜出 token 发起 update：输家主体必被围栏，赢家写入成功。
        winner_stub, loser_stub = (stub_a, stub_b) if results[0].accepted else (stub_b, stub_a)
        update_winner = winner_stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token=winner_token,
                values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(11))],
            )
        )
        update_loser = loser_stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token=winner_token,
                values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(22))],
            )
        )
        winner_reply, loser_outcome = await asyncio.gather(
            update_winner, update_loser, return_exceptions=True
        )
        assert winner_reply.results[0].success
        assert isinstance(loser_outcome, grpc.aio.AioRpcError)
        assert loser_outcome.code() is grpc.StatusCode.FAILED_PRECONDITION
        # 最终存储只可能是赢家的值，输家的 22 绝不落库。
        assert await _read_admin_value(harness) == 11
    finally:
        await channel_a.close()
        await channel_b.close()
