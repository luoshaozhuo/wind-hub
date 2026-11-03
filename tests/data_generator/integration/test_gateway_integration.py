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
from data_generator.infrastructure.config import load_settings
from data_generator.infrastructure.gateway import GatewayWriter
from data_generator.infrastructure.snapshots import recover_transaction

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


async def test_update_then_read_roundtrip(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        response = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="gw-a",
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
    finally:
        await channel.close()


async def test_per_item_results_and_duplicate_target(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        response = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="gw-a",
                values=[
                    gw.VariableUpdate(
                        variable_id="GW01.pitch_angle",
                        value=common.ScalarValue(double_value=1.0),
                    ),
                    gw.VariableUpdate(
                        variable_id="GW01.unknown",
                        value=common.ScalarValue(double_value=1.0),
                    ),
                    gw.VariableUpdate(
                        variable_id="GW01.cmd_uint",
                        value=common.ScalarValue(uint64_value=1),
                    ),
                    gw.VariableUpdate(
                        variable_id="GW01.pitch_angle",
                        value=common.ScalarValue(double_value=2.0),
                    ),
                ],
            )
        )
        result_by_id = {
            item.variable_id: item
            for item in response.results
            if item.variable_id in {"GW01.unknown", "GW01.cmd_uint"}
        }
        assert not result_by_id["GW01.unknown"].success
        assert result_by_id["GW01.unknown"].error_code == "INVALID_VARIABLE"
        assert not result_by_id["GW01.cmd_uint"].success
        # 同批重复目标第一次成功、第二次失败（逐项结果，不用 dict 折叠）。
        duplicate_results = [
            item for item in response.results if item.variable_id == "GW01.pitch_angle"
        ]
        assert len(duplicate_results) == 2
        assert [item.success for item in duplicate_results] == [True, False]
    finally:
        await channel.close()


async def test_batch_size_limits_abort(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        for values in (
            [],
            [
                gw.VariableUpdate(
                    variable_id="GW01.pitch_angle",
                    value=common.ScalarValue(double_value=1.0),
                )
                for _ in range(5001)
            ],
        ):
            with pytest.raises(grpc.RpcError) as info:
                await stub.UpdateVariables(
                    gw.UpdateVariablesRequest(gateway_id="gw-a", values=values)
                )
            assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()


async def test_ip_allowlist_denies_unauthorized_writer(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(
        endpoint_id="gw-a", devices=GW_DEVICES, data_writers=("127.0.0.2",)
    )
    channel, stub = await _stub(gateway.data_bind)
    try:
        listed = await stub.ListVariables(gw.ListVariablesRequest(gateway_id="gw-a"))
        assert listed.variables
        with pytest.raises(grpc.RpcError) as info:
            await stub.UpdateVariables(
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
        await channel.close()


async def test_gateway_lifecycle_rpc_and_revision_conflict(start_real_gateway) -> None:  # noqa: ANN001
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=GW_DEVICES)
    channel, stub = await _stub(gateway.data_bind)
    try:
        info = await stub.GetGateway(common.ObjectId(id="gw-a"))
        assert info.gateway.actual_state == "RUNNING"
        conflict = await stub.StopGateway(
            common.OperationRequest(id="gw-a", expected_revision="deadbeef")
        )
        assert conflict.accepted is False
        assert conflict.actual_state == "CONFLICT"
        stopped = await stub.StopGateway(common.OperationRequest(id="gw-a"))
        assert stopped.accepted
        assert stopped.actual_state == "STOPPED"
        restarted = await stub.StartGateway(common.OperationRequest(id="gw-a"))
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
