"""正式 ``IndustrialGatewayService`` 七 RPC 契约测试（替代已退役 gateway_pb2 旧测试）。

真实 assemble_gateway 全栈（pymodbus 协议栈 + 正式 gRPC 数据服务），
不使用任何测试替身实现 UpdateVariables；仅通过真实 grpc.aio 客户端调用。
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
                gw.ListVariablesRequest(
                    gateway_id="modbus_main", page_size=5, page_token=token
                )
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
        result = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="modbus_main",
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


async def test_update_rejects_unknown_command_duplicate_and_batch(data_gateway) -> None:
    bind, _harness = data_gateway
    channel, stub = await _stub(bind)
    try:
        response = await stub.UpdateVariables(
            gw.UpdateVariablesRequest(
                gateway_id="modbus_main",
                values=[
                    gw.VariableUpdate(
                        variable_id="turbine_001.fb_float",
                        value=common.ScalarValue(double_value=1.0),
                    ),
                    gw.VariableUpdate(
                        variable_id="turbine_001.missing",
                        value=common.ScalarValue(double_value=1.0),
                    ),
                    gw.VariableUpdate(
                        variable_id="turbine_001.cmd_uint",
                        value=common.ScalarValue(uint64_value=1),
                    ),
                    gw.VariableUpdate(
                        variable_id="turbine_001.fb_float",
                        value=common.ScalarValue(double_value=2.0),
                    ),
                ],
            )
        )
        ordered = [item for item in response.results if item.variable_id == "turbine_001.fb_float"]
        assert [item.success for item in ordered] == [True, False]
        assert all(
            not item.success
            for item in response.results
            if item.variable_id in {"turbine_001.missing", "turbine_001.cmd_uint"}
        )
        for values in ([], [
            gw.VariableUpdate(
                variable_id="turbine_001.fb_float",
                value=common.ScalarValue(double_value=1.0),
            )
            for _ in range(5001)
        ]):
            with pytest.raises(grpc.RpcError) as bad:
                await stub.UpdateVariables(
                    gw.UpdateVariablesRequest(gateway_id="modbus_main", values=values)
                )
            assert bad.value.code() == grpc.StatusCode.INVALID_ARGUMENT
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
