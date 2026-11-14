"""UC-03 集成：真实 Industrial Gateway（assemble_gateway 全栈）七类场景。

生产推送只走正式 ``IndustrialGatewayService.UpdateVariables``（无任何替身）：
ListVariables 预检、逐项结果、ReadVariables 真实读回、IP 白名单、生命周期。
"""

from __future__ import annotations

from datetime import UTC, datetime

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from data_generator.domain.model import DataBatch, DataPoint
from data_generator.infrastructure.config.service_settings import load_settings
from data_generator.infrastructure.adapters.outbound.gateway import GatewayWriter
from data_generator.infrastructure.persistence.snapshots import recover_transaction

from ..conftest import ship_scenarios_document

pytestmark = [pytest.mark.integration, pytest.mark.reliability, pytest.mark.network_failure]

GW_DEVICES = {
    "GW01": [
        ("pitch_angle", "float32", "feedback"),
        ("pitch_copy", "float32", "feedback"),
        ("cmd_uint", "uint16", "command"),
    ]
}


async def _stub(bind: str):  # noqa: ANN202
    channel = grpc.aio.insecure_channel(bind)
    await channel.channel_ready()
    return channel, gw_rpc.IndustrialGatewayServiceStub(channel)


async def test_catalog_paging_filter_and_metadata(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        names: list[str] = []
        token = ""
        while True:
            response = await stub.ListVariables(
                gw.ListVariablesRequest(gateway_id="gw-a", page_size=1, page_token=token)
            )
            assert len(response.variables) <= 1
            names.extend(item.variable_id for item in response.variables)
            token = response.next_page_token
            if not token:
                break
        assert set(names) == {"GW01.pitch_angle", "GW01.pitch_copy", "GW01.cmd_uint"}
        filtered = await stub.ListVariables(
            gw.ListVariablesRequest(gateway_id="gw-a", filter="pitch")
        )
        assert {item.variable_id for item in filtered.variables} == {
            "GW01.pitch_angle",
            "GW01.pitch_copy",
        }
    finally:
        await channel.close()


async def gw_list(stub, gateway_id: str, variable_id: str) -> gw.Variable:  # noqa: ANN001
    response = await stub.ListVariables(
        gw.ListVariablesRequest(gateway_id=gateway_id, filter=variable_id)
    )
    item = next(v for v in response.variables if v.variable_id == variable_id)
    assert isinstance(item, gw.Variable)
    return item


async def _claim(
    stub,
    gateway_id: str,
    owner_id: str,
    variable_ids: list[str],
    *,
    action: str = "ACQUIRE",
    token: str = "",
    lease_ms: int = 10000,
) -> gw.ClaimTargetsResponse:  # noqa: ANN001
    response: gw.ClaimTargetsResponse = await stub.ClaimTargets(
        gw.ClaimTargetsRequest(
            gateway_id=gateway_id,
            owner_id=owner_id,
            variable_ids=variable_ids,
            action=action,
            lease_token=token,
            lease_ms=lease_ms,
        )
    )
    return response


async def test_update_then_read_roundtrip(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        # 冻结契约（设计 v1_23 R01-04）：目标无人认领不构成无令牌写入许可，
        # 无令牌注入必须被 FAILED_PRECONDITION/LEASE_LOST 整批拒绝。
        with pytest.raises(grpc.RpcError) as unclaimed:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=7.25),
                        )
                    ],
                )
            )
        assert unclaimed.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        claim = await _claim(stub, "gw-a", "owner-a", ["GW01.pitch_angle"])
        assert claim.accepted and claim.lease_token
        # 租约生效后，无令牌写入同目标必须被整批围栏拒绝，不存在兼容旁路。
        with pytest.raises(grpc.RpcError) as fenced:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=7.5),
                        )
                    ],
                )
            )
        assert fenced.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        response = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="gw-a",
                lease_token=claim.lease_token,
                values=[
                    gw.VariableUpdate(
                        variable_id="GW01.pitch_angle",
                        value=common.ScalarValue(double_value=8.75),
                    )
                ],
            )
        )
        assert len(response.results) == 1
        assert response.results[0].success
        read = await stub.ReadVariables(
            gw.ReadVariablesRequest(gateway_id="gw-a", variable_ids=["GW01.pitch_angle"])
        )
        assert read.values[0].quality == "GOOD"
        assert read.values[0].HasField("value")
        assert read.values[0].value.double_value == pytest.approx(8.75)
        release = await _claim(
            stub,
            "gw-a",
            "owner-a",
            ["GW01.pitch_angle"],
            action="RELEASE",
            token=claim.lease_token,
        )
        assert release.accepted
        # 释放后同一令牌立即失效。
        with pytest.raises(grpc.RpcError) as after_release:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    lease_token=claim.lease_token,
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=9.0),
                        )
                    ],
                )
            )
        assert after_release.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()


async def test_unclaimed_update_rejected(start_real_gateway) -> None:  # noqa: ANN001
    """UR-03/UC-03-S02：无 lease_token 且目标空闲时，真实 Gateway 仍整批拒绝，值无变化。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        with pytest.raises(grpc.RpcError) as denied:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=3.5),
                        )
                    ],
                )
            )
        assert denied.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        readback = await stub.ReadVariables(
            gw.ReadVariablesRequest(gateway_id="gw-a", variable_ids=["GW01.pitch_angle"])
        )
        assert readback.values[0].quality == "UNKNOWN"
        assert not readback.values[0].HasField("value")
    finally:
        await channel.close()


async def test_per_item_results_and_duplicate_target(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        # v1 租约在申请阶段即拒绝目录外目标（整组原子，错误码 INVALID_TARGET）。
        claim_command = await _claim(stub, "gw-a", "owner-a", ["GW01.unknown"])
        assert not claim_command.accepted
        assert claim_command.error_code == "INVALID_TARGET"
        claim = await _claim(stub, "gw-a", "owner-a", ["GW01.pitch_angle", "GW01.pitch_copy"])
        assert claim.accepted
        response = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="gw-a",
                lease_token=claim.lease_token,
                values=[
                    gw.VariableUpdate(
                        variable_id="GW01.pitch_angle",
                        value=common.ScalarValue(double_value=1.0),
                    ),
                    gw.VariableUpdate(
                        variable_id="GW01.pitch_copy",
                        value=common.ScalarValue(double_value=2.0),
                    ),
                ],
            )
        )
        assert {item.variable_id: item.success for item in response.results} == {
            "GW01.pitch_angle": True,
            "GW01.pitch_copy": True,
        }
        # 租约外未知点混入已租约批次：服务端整批围栏拒绝，不产生任何写入。
        with pytest.raises(grpc.RpcError) as unknown:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    lease_token=claim.lease_token,
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=3.0),
                        ),
                        gw.VariableUpdate(
                            variable_id="GW01.unknown",
                            value=common.ScalarValue(double_value=1.0),
                        ),
                    ],
                )
            )
        assert unknown.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        # v1_10 服务端对同批重复目标整批 INVALID_ARGUMENT 拒绝（原子批，不逐项折叠）。
        with pytest.raises(grpc.RpcError) as duplicate:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    lease_token=claim.lease_token,
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=1.0),
                        ),
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=2.0),
                        ),
                    ],
                )
            )
        assert duplicate.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()


async def test_batch_shape_and_claim_size_limits(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        # 冻结契约：无令牌的任何 UpdateVariables（含空批）一律整批拒绝。
        with pytest.raises(grpc.RpcError) as empty_denied:
            await stub.UpdateVariables(gw.UpdateVariablesRequest(gateway_id="gw-a", values=[]))
        assert empty_denied.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        # 持有有效租约时，空批仍是合法无操作，返回空结果集且不伪造错误。
        noop_claim = await _claim(stub, "gw-a", "owner-noop", ["GW01.pitch_angle"])
        assert noop_claim.accepted
        empty = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="gw-a",
                lease_token=noop_claim.lease_token,
                values=[],
            )
        )
        assert empty.results == []
        # 租约申请的 5000 目标上限：超限整组 INVALID_CLAIM 拒绝，不产生租约。
        oversized = await _claim(
            stub,
            "gw-a",
            "owner-oversize",
            [f"GW01.var_{index}" for index in range(5001)],
        )
        assert not oversized.accepted
        assert oversized.error_code == "INVALID_CLAIM"
        # 同批重复目标在任何写入前被 INVALID_ARGUMENT 整批拒绝。
        with pytest.raises(grpc.RpcError) as info:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=1.0),
                        ),
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=2.0),
                        ),
                    ],
                )
            )
        assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()


async def test_mtls_denies_unauthorized_writer(start_real_gateway) -> None:  # noqa: ANN001
    # v1 安全模型：明文仅限 loopback；跨网络写入必须 mTLS，授权边界是服务端按
    # 客户端证书 CN 配置的权限集。无权限身份在读取/租约/写入三个入口一律拒绝。
    gateway = await start_real_gateway(
        endpoint_id="gw-a",
        devices=GW_DEVICES,
        permissions={"generator-a": ["read", "inject"]},
        client_names=("stranger",),
    )
    denied_channel = gateway.mtls_channel("stranger")
    denied_stub = gw_rpc.IndustrialGatewayServiceStub(denied_channel)
    try:
        with pytest.raises(grpc.RpcError) as read_denied:
            await denied_stub.ListVariables(gw.ListVariablesRequest(gateway_id="gw-a"))
        assert read_denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # 安全边界在 ClaimTargets，而非只靠写入端自觉。
        with pytest.raises(grpc.RpcError) as claim_denied:
            await _claim(denied_stub, "gw-a", "owner-x", ["GW01.pitch_angle"])
        assert claim_denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as info:
            await denied_stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-a",
                    values=[
                        gw.VariableUpdate(
                            variable_id="GW01.pitch_angle",
                            value=common.ScalarValue(double_value=1.0),
                        )
                    ],
                )
            )
        assert info.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await denied_channel.close()

    # 正面对照：持有 inject 权限的身份可正常申请租约，证明拒绝来自权限而非链路故障。
    authorized_channel = gateway.mtls_channel("generator-a")
    authorized_stub = gw_rpc.IndustrialGatewayServiceStub(authorized_channel)
    try:
        listed = await authorized_stub.ListVariables(gw.ListVariablesRequest(gateway_id="gw-a"))
        assert listed.variables
        claim = await _claim(authorized_stub, "gw-a", "owner-a", ["GW01.pitch_angle"])
        assert claim.accepted
    finally:
        await authorized_channel.close()


async def test_gateway_lifecycle_rpc_and_revision_conflict(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        info = await stub.GetGateway(common.ObjectId(id="gw-a"))
        assert info.gateway.actual_state == "RUNNING"
        revision = info.gateway.revision
        assert revision
        # v1 强制 expected_revision：缺失 INVALID_ARGUMENT，错误版本 ABORTED。
        with pytest.raises(grpc.RpcError) as missing_revision:
            await stub.StopGateway(common.OperationRequest(id="gw-a"))
        assert missing_revision.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.RpcError) as conflict:
            await stub.StopGateway(common.OperationRequest(id="gw-a", expected_revision="deadbeef"))
        assert conflict.value.code() == grpc.StatusCode.ABORTED
        stopped = await stub.StopGateway(
            common.OperationRequest(id="gw-a", expected_revision=revision)
        )
        assert stopped.accepted
        assert stopped.actual_state == "STOPPED"
        restarted = await stub.StartGateway(
            common.OperationRequest(id="gw-a", expected_revision=revision)
        )
        assert restarted.actual_state == "RUNNING"
        with pytest.raises(grpc.RpcError) as missing:
            await stub.GetGateway(common.ObjectId(id="nope"))
        assert missing.value.code() == grpc.StatusCode.NOT_FOUND
    finally:
        await channel.close()


async def test_dg_writer_uses_formal_update_rpc_end_to_end(
    start_real_gateway,
    configs_root,  # noqa: ANN001
) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            mappings=[("gw-a", "GW01.pitch_angle")],
        ),
        output_timeout=3.0,
        max_inflight=4,
    )
    recover_transaction(service_path.absolute().parent.parent)
    settings = load_settings(service_path)
    scenario = settings.scenarios["demo-random"]
    writer = GatewayWriter(timeout=3.0, max_inflight=4)
    try:
        await writer.validate(scenario)
        await writer.claim(scenario, "gen-x")
        now = datetime.now(UTC)
        batch = DataBatch(
            "gen-x",
            1,
            now,
            (DataPoint("GW01", "a02_dm_pitchangle1", 11.25, now),),
        )
        results = await writer.send(scenario, batch)
        assert len(results) == 1
        assert results[0].state == "SUCCESS"
    finally:
        await writer.release(scenario, "gen-x")
        assert await writer.close()

    channel, stub = await _stub(gateway.data_bind)
    try:
        read = await stub.ReadVariables(
            gw.ReadVariablesRequest(gateway_id="gw-a", variable_ids=["GW01.pitch_angle"])
        )
        assert read.values[0].value.double_value == pytest.approx(11.25)
    finally:
        await channel.close()


async def test_dg_writer_preflight_rejects_type_mismatch(
    start_real_gateway,
    configs_root,  # noqa: ANN001
) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    document = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        mappings=[("gw-a", "GW01.pitch_angle")],
    )
    # 把源点类型改为字符串：真实目录 float32 与期望 str 不一致必须预检拒绝。
    document["scenarios"][0]["devices"][0]["points"][0]["model"] = {
        "kind": "random",
        "distribution": "uniform",
        "minimum": 0,
        "maximum": 10,
    }
    service_path = configs_root(scenarios=document)
    recover_transaction(service_path.absolute().parent.parent)
    settings = load_settings(service_path)
    # 直接改写 scenario 点类型模拟服务端类型不一致（目录仍是 float32）。
    scenario = settings.scenarios["demo-random"]
    scenario.points[("GW01", "a02_dm_pitchangle1")]["data_type"] = "str"
    writer = GatewayWriter(timeout=3.0)
    try:
        with pytest.raises(Exception, match="expected writable str"):
            await writer.validate(scenario)
    finally:
        await writer.close()
