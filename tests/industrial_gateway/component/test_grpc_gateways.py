"""真实 grpc.aio 通道验证 Gateway 查询/生命周期 4 RPC 与 CommonService 基础面。

不使用 Mock：启动生产 create_gateway_grpc_server 与真实 runtime，
通过真实 loopback HTTP/2 通道调用。
"""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as pb
from tests.support.gateway_v1 import (
    FEEDBACK_FLOAT,
    ServerSpec,
    reserve_port,
    start_gateway_v1,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service]


def _spec(server_id: str, device: str, *, state: str = "START") -> ServerSpec:
    return ServerSpec(
        server_id=server_id,
        device_id=device,
        modbus_port=reserve_port(),
        slave_id=1,
        desired_state=state,
        control_enabled=False,
        command_mappings=(),
    )


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(
        tmp_path / "root",
        [_spec("gw1", "D01"), _spec("gw2", "D02"), _spec("gw3", "D03")],
    )
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_list_and_get_gateway_reflect_running_state(harness) -> None:
    stub = harness.gateway_stub()
    listed = await stub.ListGateways(pb.ListGatewaysRequest())
    assert [item.gateway_id for item in listed.gateways] == ["gw1", "gw2", "gw3"]
    assert all(item.actual_state == "RUNNING" for item in listed.gateways)
    assert all(item.revision for item in listed.gateways)
    one = await stub.GetGateway(common.ObjectId(id="gw1"))
    assert one.gateway.endpoint == f"127.0.0.1:{harness.specs['gw1'].modbus_port}"
    assert "15 variables" in one.gateway.mapping_summary


async def test_get_unknown_gateway_is_not_found(harness) -> None:
    stub = harness.gateway_stub()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetGateway(common.ObjectId(id="nope"))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_list_filter_and_pagination(harness) -> None:
    stub = harness.gateway_stub()
    filtered = await stub.ListGateways(pb.ListGatewaysRequest(filter="gw2"))
    assert [item.gateway_id for item in filtered.gateways] == ["gw2"]

    page1 = await stub.ListGateways(pb.ListGatewaysRequest(page_size=2))
    assert [item.gateway_id for item in page1.gateways] == ["gw1", "gw2"]
    assert page1.next_page_token == "2"
    page2 = await stub.ListGateways(
        pb.ListGatewaysRequest(page_size=2, page_token=page1.next_page_token)
    )
    assert [item.gateway_id for item in page2.gateways] == ["gw3"]
    assert page2.next_page_token == ""

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ListGateways(pb.ListGatewaysRequest(page_token="abc"))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_stop_and_start_gateway_cas(harness) -> None:
    stub = harness.gateway_stub()
    revision = (await stub.GetGateway(common.ObjectId(id="gw1"))).gateway.revision

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.StopGateway(common.OperationRequest(id="gw1", expected_revision="stale"))
    assert exc.value.code() is grpc.StatusCode.ABORTED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.StopGateway(common.OperationRequest(id="gw1"))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    stopped = await stub.StopGateway(common.OperationRequest(id="gw1", expected_revision=revision))
    assert stopped.accepted
    assert stopped.actual_state == "STOPPED"
    # 幂等：STOPPED 上再停仍成功。
    again = await stub.StopGateway(common.OperationRequest(id="gw1", expected_revision=revision))
    assert again.accepted
    restarted = await stub.StartGateway(
        common.OperationRequest(id="gw1", expected_revision=revision)
    )
    assert restarted.accepted and restarted.actual_state == "RUNNING"
    # RUNNING 上再启动为无操作成功。
    idempotent = await stub.StartGateway(
        common.OperationRequest(id="gw1", expected_revision=revision)
    )
    assert idempotent.accepted


async def test_start_stop_unknown_gateway_not_found(harness) -> None:
    stub = harness.gateway_stub()
    revision = harness.runtime.config.revision
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.StartGateway(common.OperationRequest(id="ghost", expected_revision=revision))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_list_variables_filter_pagination_and_address(harness) -> None:
    stub = harness.gateway_stub()
    variables = await stub.ListVariables(pb.ListVariablesRequest(gateway_id="gw1"))
    names = {item.variable_id for item in variables.variables}
    assert FEEDBACK_FLOAT in names and len(names) == 15
    float_var = next(item for item in variables.variables if item.variable_id == FEEDBACK_FLOAT)
    assert float_var.data_type == "float32"
    address = {field.key: field.value for field in float_var.address}
    assert address["address"].WhichOneof("kind") is not None

    page = await stub.ListVariables(pb.ListVariablesRequest(gateway_id="gw1", page_size=5))
    assert len(page.variables) == 5 and page.next_page_token
    filtered = await stub.ListVariables(pb.ListVariablesRequest(gateway_id="gw1", filter="cmd_"))
    assert {item.variable_id for item in filtered.variables} == {
        "cmd_float",
        "cmd_uint2",
        "cmd_uint",
        "cmd_coil",
        "cmd_coil_b",
    }
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ListVariables(pb.ListVariablesRequest(gateway_id="ghost"))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_read_variables_quality_states(harness) -> None:
    stub = harness.gateway_stub()
    empty = await stub.ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_FLOAT])
    )
    sample = empty.values[0]
    assert sample.quality == "UNKNOWN"
    assert sample.freshness == "UNINITIALIZED"
    assert not sample.HasField("value")

    unknown = await stub.ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=["no_such_point"])
    )
    assert unknown.values[0].quality == "BAD"
    assert unknown.values[0].error_code == "NOT_FOUND"

    all_vars = await stub.ReadVariables(pb.ReadVariablesRequest(gateway_id="gw1"))
    assert len(all_vars.values) == 15


async def test_service_info_capabilities_and_status(harness) -> None:
    common_stub = harness.common_stub()
    info = await common_stub.GetServiceInfo(common.Empty())
    assert info.service_id == "industrial_gateway"
    caps = await common_stub.GetCapabilities(common.Empty())
    # Common 9 个 RPC + IndustrialGateway 10 个 RPC（含两个仿真执行 RPC）。
    assert len(caps.rpc_names) == 19
    assert "CommonService.Diagnose" in caps.rpc_names
    assert "IndustrialGatewayService.ClaimTargets" in caps.rpc_names
    assert "modbus_tcp" in caps.protocols
    status = await common_stub.GetServiceStatus(common.Empty())
    gateways = {item.name: item.health for item in status.dependencies}
    assert gateways["gw1"] == "HEALTHY"
    assert gateways["metrics"] in {"HEALTHY", "NOT_READY"}
