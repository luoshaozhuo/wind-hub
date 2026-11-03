"""正式 ``IndustrialGatewayService`` 八 RPC 契约测试（v1_10 含 ClaimTargets 租约）。

真实 assemble_gateway 全栈（pymodbus 协议栈 + 正式 gRPC 数据服务），
不使用任何测试替身实现 UpdateVariables/ClaimTargets；仅通过真实 grpc.aio 客户端。
v1_10：UpdateVariables 必须携带 ClaimTargets 签发的 fencing token，
不存在旧的无令牌写入兼容路径。
"""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from tests.support.gateway_harness import (
    UNAUTHORIZED_IP,
    FakeCommanderService,
    build_config,
    reserve_port,
    start_gateway,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


async def _stub(bind: str):  # noqa: ANN202
    channel = grpc.aio.insecure_channel(bind)
    await channel.channel_ready()
    return channel, gw_rpc.IndustrialGatewayServiceStub(channel)


async def _claim(  # noqa: ANN202
    stub,  # noqa: ANN001
    owner: str,
    variables: list[str],
    *,
    action: str = "ACQUIRE",
    token: str = "",
    lease_ms: int = 10_000,
):
    return await stub.ClaimTargets(
        gw.ClaimTargetsRequest(
            gateway_id="modbus_main",
            owner_id=owner,
            variable_ids=variables,
            action=action,
            lease_token=token,
            lease_ms=lease_ms,
        )
    )


@pytest.fixture
async def data_gateway(tmp_path: Path):
    bind = f"127.0.0.1:{reserve_port()}"
    config = build_config(
        reserve_port(),
        reserve_port(),
        tmp_path / "journal.json",
        units=[1],
        data_writers=(UNAUTHORIZED_IP,),
        control_enabled=False,
    )
    config = config.model_copy(update={"data_grpc_bind": bind})
    harness = await start_gateway(config, FakeCommanderService())
    yield bind, harness
    await harness.stop()


async def test_list_and_get_gateway(data_gateway) -> None:
    bind, harness = data_gateway
    channel, stub = await _stub(bind)
    try:
        response = await stub.ListGateways(gw.ListGatewaysRequest())
        assert [item.gateway_id for item in response.gateways] == ["modbus_main"]
        one = await stub.GetGateway(common.ObjectId(id="modbus_main"))
        assert one.gateway.actual_state == "RUNNING"
        with pytest.raises(grpc.RpcError) as missing:
            await stub.GetGateway(common.ObjectId(id="ghost"))
        assert missing.value.code() == grpc.StatusCode.NOT_FOUND
    finally:
        await channel.close()


async def test_lifecycle_and_revision_conflict(data_gateway) -> None:
    bind, _harness = data_gateway
    channel, stub = await _stub(bind)
    try:
        conflict = await stub.StopGateway(
            common.OperationRequest(id="modbus_main", expected_revision="stale")
        )
        assert conflict.actual_state == "CONFLICT"
        stopped = await stub.StopGateway(common.OperationRequest(id="modbus_main"))
        assert stopped.accepted
        assert stopped.actual_state == "STOPPED"
        started = await stub.StartGateway(common.OperationRequest(id="modbus_main"))
        assert started.actual_state == "RUNNING"
    finally:
        await channel.close()


async def test_variable_catalog_roles_and_paging(data_gateway) -> None:
    bind, _harness = data_gateway
    channel, stub = await _stub(bind)
    try:
        names: dict[str, gw.Variable] = {}
        token = ""
        while True:
            response = await stub.ListVariables(
                gw.ListVariablesRequest(gateway_id="modbus_main", page_size=5, page_token=token)
            )
            for item in response.variables:
                names[item.variable_id] = item
            token = response.next_page_token
            if not token:
                break
        feedback = names["turbine_001.fb_float"]
        command = names["turbine_001.cmd_uint"]
        assert feedback.writable is True
        assert feedback.data_type == "float32"
        assert command.writable is False
    finally:
        await channel.close()


async def test_read_variables_no_value_then_update_and_readback(data_gateway) -> None:
    bind, _harness = data_gateway
    channel, stub = await _stub(bind)
    variable = "turbine_001.fb_float"
    try:
        empty = await stub.ReadVariables(
            gw.ReadVariablesRequest(gateway_id="modbus_main", variable_ids=[variable])
        )
        assert empty.values[0].quality == "UNKNOWN"
        assert empty.values[0].error_code == "NO_VALUE"
        # v1_10：先 ClaimTargets 取得 fencing token，写入必须携带。
        lease = await _claim(stub, "owner-1", [variable])
        assert lease.accepted and lease.lease_token
        result = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="modbus_main",
                lease_token=lease.lease_token,
                values=[
                    gw.VariableUpdate(
                        variable_id=variable,
                        value=common.ScalarValue(double_value=3.25),
                    )
                ],
            )
        )
        assert result.results[0].success
        read = await stub.ReadVariables(
            gw.ReadVariablesRequest(gateway_id="modbus_main", variable_ids=[variable])
        )
        assert read.values[0].quality == "GOOD"
        assert read.values[0].value.double_value == pytest.approx(3.25)
    finally:
        await channel.close()


async def test_update_fencing_duplicate_and_batch_limits(data_gateway) -> None:
    bind, _harness = data_gateway
    channel, stub = await _stub(bind)
    try:
        # v1_10：命令点不是 feedback，申请阶段即拒绝；不能再走逐项写入失败。
        command_claim = await _claim(stub, "owner-1", ["turbine_001.cmd_uint"])
        assert not command_claim.accepted
        assert command_claim.error_code == "INVALID_TARGETS"

        lease = await _claim(stub, "owner-1", ["turbine_001.fb_float"])
        assert lease.accepted

        # 无令牌写入：服务端围栏直接拒绝，不存在兼容旁路。
        with pytest.raises(grpc.RpcError) as no_token:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="modbus_main",
                    values=[
                        gw.VariableUpdate(
                            variable_id="turbine_001.fb_float",
                            value=common.ScalarValue(double_value=1.0),
                        )
                    ],
                )
            )
        assert no_token.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        # 租约外未知点混入：整批围栏拒绝，不产生任何部分写入。
        with pytest.raises(grpc.RpcError) as unknown:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="modbus_main",
                    lease_token=lease.lease_token,
                    values=[
                        gw.VariableUpdate(
                            variable_id="turbine_001.fb_float",
                            value=common.ScalarValue(double_value=1.0),
                        ),
                        gw.VariableUpdate(
                            variable_id="turbine_001.missing",
                            value=common.ScalarValue(double_value=1.0),
                        ),
                    ],
                )
            )
        assert unknown.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        # 同批重复目标：整批 INVALID_ARGUMENT（原子批，不逐项折叠）。
        with pytest.raises(grpc.RpcError) as duplicate:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="modbus_main",
                    lease_token=lease.lease_token,
                    values=[
                        gw.VariableUpdate(
                            variable_id="turbine_001.fb_float",
                            value=common.ScalarValue(double_value=1.0),
                        ),
                        gw.VariableUpdate(
                            variable_id="turbine_001.fb_float",
                            value=common.ScalarValue(double_value=2.0),
                        ),
                    ],
                )
            )
        assert duplicate.value.code() == grpc.StatusCode.INVALID_ARGUMENT

        for values in (
            [],
            [
                gw.VariableUpdate(
                    variable_id="turbine_001.fb_float",
                    value=common.ScalarValue(double_value=1.0),
                )
                for _ in range(5001)
            ],
        ):
            with pytest.raises(grpc.RpcError) as bad:
                await stub.UpdateVariables(
                    gw.UpdateVariablesRequest(
                        gateway_id="modbus_main",
                        lease_token=lease.lease_token,
                        values=values,
                    )
                )
            assert bad.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()


async def test_claim_targets_conflict_renew_release_and_fencing(data_gateway) -> None:
    bind, _harness = data_gateway
    channel, stub = await _stub(bind)
    variable = "turbine_001.fb_float"
    try:
        lease = await _claim(stub, "owner-a", [variable], lease_ms=10_000)
        assert lease.accepted and lease.lease_token
        # 第二方整组冲突；非法 lease_ms / 携带 token 的 ACQUIRE 都被拒绝。
        busy = await _claim(stub, "owner-b", [variable])
        assert not busy.accepted and busy.error_code == "TARGET_BUSY"
        bad_ttl = await _claim(stub, "owner-b", [variable], lease_ms=500)
        assert not bad_ttl.accepted and bad_ttl.error_code == "INVALID_LEASE"
        # 伪造 owner 的 RENEW 被 LEASE_LOST 拒绝，真 owner 续租成功。
        forged = await _claim(stub, "forger", [variable], action="RENEW", token=lease.lease_token)
        assert not forged.accepted and forged.error_code == "LEASE_LOST"
        renewed = await _claim(
            stub,
            "owner-a",
            [variable],
            action="RENEW",
            token=lease.lease_token,
            lease_ms=10_000,
        )
        assert renewed.accepted
        # RELEASE 后旧 token 立即失效，第二方可以取得。
        released = await _claim(
            stub, "owner-a", [variable], action="RELEASE", token=lease.lease_token
        )
        assert released.accepted
        with pytest.raises(grpc.RpcError) as fenced:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="modbus_main",
                    lease_token=lease.lease_token,
                    values=[
                        gw.VariableUpdate(
                            variable_id=variable,
                            value=common.ScalarValue(double_value=5.0),
                        )
                    ],
                )
            )
        assert fenced.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        acquired = await _claim(stub, "owner-b", [variable])
        assert acquired.accepted
    finally:
        await channel.close()


async def test_update_denied_for_unconfigured_peer(tmp_path: Path) -> None:
    bind = f"127.0.0.1:{reserve_port()}"
    # 默认 data_writers 仅含 Collector 127.0.0.2，不含本测试客户端 127.0.0.1。
    config = build_config(
        reserve_port(),
        reserve_port(),
        tmp_path / "journal.json",
        units=[1],
        control_enabled=False,
    )
    config = config.model_copy(update={"data_grpc_bind": bind})
    harness = await start_gateway(config, FakeCommanderService())
    channel, stub = await _stub(bind)
    try:
        # v1_10：非白名单连租约申请都必须拒绝，安全边界在 ClaimTargets。
        with pytest.raises(grpc.RpcError) as claim_denied:
            await _claim(stub, "owner-x", ["turbine_001.fb_float"])
        assert claim_denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as denied:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="modbus_main",
                    values=[
                        gw.VariableUpdate(
                            variable_id="turbine_001.fb_float",
                            value=common.ScalarValue(double_value=1.0),
                        )
                    ],
                )
            )
        assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()
        await harness.stop()
