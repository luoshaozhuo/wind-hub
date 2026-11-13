"""50 个 RPC 的 Service Descriptor 矩阵：6 个 Service 各含 9/11/7/4/10/9 个 Unary。"""

from __future__ import annotations

from types import ModuleType

import pytest
from google.protobuf.descriptor import FieldDescriptor, MethodDescriptor

from contracts import (
    collector_pb2,
    commander_pb2,
    common_pb2,
    data_generator_pb2,
    industrial_gateway_pb2,
    sink_service_pb2,
)

pytestmark = pytest.mark.contract

C = "windhub.common.v1."

# package, service, [(rpc, 输入全名, 输出全名)]
EXPECTED: dict[ModuleType, tuple[str, str, list[tuple[str, str, str]]]] = {
    common_pb2: (
        "windhub.common.v1",
        "CommonService",
        [
            ("GetServiceInfo", f"{C}Empty", f"{C}GetServiceInfoResponse"),
            ("GetServiceStatus", f"{C}Empty", f"{C}GetServiceStatusResponse"),
            ("GetCapabilities", f"{C}Empty", f"{C}GetCapabilitiesResponse"),
            (
                "GetConfigSnapshot",
                f"{C}GetConfigSnapshotRequest",
                f"{C}GetConfigSnapshotResponse",
            ),
            (
                "PushConfigSnapshot",
                f"{C}PushConfigSnapshotRequest",
                f"{C}PushConfigSnapshotResponse",
            ),
            (
                "ApplyConfigSnapshot",
                f"{C}ApplyConfigSnapshotRequest",
                f"{C}ApplyConfigSnapshotResponse",
            ),
            ("GetMetrics", f"{C}GetMetricsRequest", f"{C}GetMetricsResponse"),
            ("GetEvents", f"{C}GetEventsRequest", f"{C}GetEventsResponse"),
            ("Diagnose", f"{C}DiagnoseRequest", f"{C}DiagnoseResponse"),
        ],
    ),
    collector_pb2: (
        "windhub.collector.v1",
        "CollectorService",
        [
            (
                "ListDevices",
                "windhub.collector.v1.ListDevicesRequest",
                "windhub.collector.v1.ListDevicesResponse",
            ),
            (
                "GetDevice",
                "windhub.collector.v1.DeviceQuery",
                "windhub.collector.v1.GetDeviceResponse",
            ),
            (
                "ListPoints",
                "windhub.collector.v1.ListPointsRequest",
                "windhub.collector.v1.ListPointsResponse",
            ),
            (
                "GetPoint",
                "windhub.collector.v1.PointQuery",
                "windhub.collector.v1.GetPointResponse",
            ),
            (
                "ReadPoints",
                "windhub.collector.v1.ReadPointsRequest",
                "windhub.collector.v1.ReadPointsResponse",
            ),
            (
                "ListTasks",
                "windhub.collector.v1.ListTasksRequest",
                "windhub.collector.v1.ListTasksResponse",
            ),
            ("GetTask", f"{C}ObjectId", "windhub.collector.v1.GetTaskResponse"),
            ("StartTask", f"{C}OperationRequest", f"{C}OperationResult"),
            ("StopTask", f"{C}OperationRequest", f"{C}OperationResult"),
            (
                "GetPublishingStatus",
                "windhub.collector.v1.GetPublishingStatusRequest",
                "windhub.collector.v1.GetPublishingStatusResponse",
            ),
            (
                "InterrogateDevice",
                "windhub.collector.v1.InterrogateDeviceRequest",
                "windhub.collector.v1.InterrogateDeviceResponse",
            ),
        ],
    ),
    commander_pb2: (
        "windhub.commander.v1",
        "CommanderService",
        [
            (
                "ListDevices",
                "windhub.commander.v1.ListDevicesRequest",
                "windhub.commander.v1.ListDevicesResponse",
            ),
            ("GetDevice", f"{C}ObjectId", "windhub.commander.v1.GetDeviceResponse"),
            (
                "ListPoints",
                "windhub.commander.v1.ListPointsRequest",
                "windhub.commander.v1.ListPointsResponse",
            ),
            (
                "SubmitCommand",
                "windhub.commander.v1.SubmitCommandRequest",
                "windhub.commander.v1.SubmitCommandResponse",
            ),
            (
                "SubmitCommands",
                "windhub.commander.v1.SubmitCommandsRequest",
                "windhub.commander.v1.SubmitCommandsResponse",
            ),
            (
                "GetCommandResult",
                "windhub.commander.v1.GetCommandResultRequest",
                "windhub.commander.v1.GetCommandResultResponse",
            ),
            (
                "GetRequestResult",
                "windhub.commander.v1.GetRequestResultRequest",
                "windhub.commander.v1.GetRequestResultResponse",
            ),
        ],
    ),
    sink_service_pb2: (
        "windhub.sink.v1",
        "SinkService",
        [
            (
                "ListDeliveries",
                "windhub.sink.v1.ListDeliveriesRequest",
                "windhub.sink.v1.ListDeliveriesResponse",
            ),
            ("GetDelivery", f"{C}ObjectId", "windhub.sink.v1.GetDeliveryResponse"),
            ("StartDelivery", f"{C}OperationRequest", f"{C}OperationResult"),
            ("StopDelivery", f"{C}OperationRequest", f"{C}OperationResult"),
        ],
    ),
    industrial_gateway_pb2: (
        "windhub.gateway.v1",
        "IndustrialGatewayService",
        [
            (
                "ListGateways",
                "windhub.gateway.v1.ListGatewaysRequest",
                "windhub.gateway.v1.ListGatewaysResponse",
            ),
            ("GetGateway", f"{C}ObjectId", "windhub.gateway.v1.GetGatewayResponse"),
            ("StartGateway", f"{C}OperationRequest", f"{C}OperationResult"),
            ("StopGateway", f"{C}OperationRequest", f"{C}OperationResult"),
            (
                "ListVariables",
                "windhub.gateway.v1.ListVariablesRequest",
                "windhub.gateway.v1.ListVariablesResponse",
            ),
            (
                "ReadVariables",
                "windhub.gateway.v1.ReadVariablesRequest",
                "windhub.gateway.v1.ReadVariablesResponse",
            ),
            (
                "UpdateVariables",
                "windhub.gateway.v1.UpdateVariablesRequest",
                "windhub.gateway.v1.UpdateVariablesResponse",
            ),
            (
                "ClaimTargets",
                "windhub.gateway.v1.ClaimTargetsRequest",
                "windhub.gateway.v1.ClaimTargetsResponse",
            ),
            (
                "ExecuteSimulationCommand",
                "windhub.gateway.v1.SimulationCommandRequest",
                "windhub.gateway.v1.SimulationCommandResponse",
            ),
            (
                "GetSimulationExecutionResult",
                "windhub.gateway.v1.SimulationExecutionQuery",
                "windhub.gateway.v1.SimulationCommandResponse",
            ),
        ],
    ),
    data_generator_pb2: (
        "windhub.data_generator.v1",
        "DataGeneratorService",
        [
            (
                "ListDevices",
                "windhub.data_generator.v1.ListDevicesRequest",
                "windhub.data_generator.v1.ListDevicesResponse",
            ),
            (
                "GetDevice",
                f"{C}ObjectId",
                "windhub.data_generator.v1.GetDeviceResponse",
            ),
            (
                "ListScenarios",
                "windhub.data_generator.v1.ListScenariosRequest",
                "windhub.data_generator.v1.ListScenariosResponse",
            ),
            (
                "GetGeneration",
                f"{C}ObjectId",
                "windhub.data_generator.v1.GetGenerationResponse",
            ),
            (
                "StartGeneration",
                "windhub.data_generator.v1.StartGenerationRequest",
                "windhub.data_generator.v1.StartGenerationResponse",
            ),
            (
                "StopGeneration",
                "windhub.data_generator.v1.StopGenerationRequest",
                "windhub.data_generator.v1.StopGenerationResponse",
            ),
            (
                "ListVariables",
                "windhub.data_generator.v1.ListVariablesRequest",
                "windhub.data_generator.v1.ListVariablesResponse",
            ),
            (
                "UpdateVariables",
                "windhub.data_generator.v1.UpdateVariablesRequest",
                "windhub.data_generator.v1.UpdateVariablesResponse",
            ),
            (
                "GetInputExecution",
                "windhub.data_generator.v1.GetInputExecutionRequest",
                "windhub.data_generator.v1.GetInputExecutionResponse",
            ),
        ],
    ),
}


def _all_methods() -> list[MethodDescriptor]:
    methods: list[MethodDescriptor] = []
    for module in EXPECTED:
        for service in module.DESCRIPTOR.services_by_name.values():
            methods.extend(service.methods)
    return methods


# 以 RPC 全名为键，允许 ListDevices/ListPoints 等同名 RPC 分属不同 Service。
EXPECTED_RPCS: dict[str, tuple[str, str]] = {
    f"{package}.{service_name}.{rpc_name}": (input_name, output_name)
    for package, service_name, rpcs in EXPECTED.values()
    for rpc_name, input_name, output_name in rpcs
}


def test_each_proto_declares_exactly_one_service_with_expected_package() -> None:
    for module, (package, service_name, _) in EXPECTED.items():
        descriptor = module.DESCRIPTOR
        assert descriptor.package == package
        assert set(descriptor.services_by_name) == {service_name}


def test_rpc_total_is_50() -> None:
    assert sum(len(rpcs) for _, _, rpcs in EXPECTED.values()) == 50
    assert len(_all_methods()) == 50


def test_data_generator_rpc_count_is_nine() -> None:
    _, _, rpcs = EXPECTED[data_generator_pb2]
    assert len(rpcs) == 9


def test_input_execution_message_field_numbers_frozen() -> None:
    # Contracts v1_27：请求级执行证据消息字段编号不可重排。
    messages = data_generator_pb2.DESCRIPTOR.message_types_by_name
    request = messages["GetInputExecutionRequest"]
    response = messages["GetInputExecutionResponse"]
    assert [field.number for field in request.fields] == [1, 2]
    assert [field.name for field in request.fields] == ["generation_id", "request_id"]
    assert [field.number for field in response.fields] == [1, 2, 3]
    assert [field.name for field in response.fields] == [
        "request_id",
        "state",
        "effective_step",
    ]
    assert response.fields_by_name["effective_step"].type == FieldDescriptor.TYPE_UINT64


def test_commander_rpc_count_is_seven() -> None:
    _, _, rpcs = EXPECTED[commander_pb2]
    assert len(rpcs) == 7


def test_commander_message_and_field_counts() -> None:
    # Contracts v1_16 §12.2：Commander 15 种消息、58 个字段，逐 Descriptor 核验。
    messages = commander_pb2.DESCRIPTOR.message_types_by_name
    assert len(messages) == 15
    assert sum(len(message.fields) for message in messages.values()) == 58


def test_commander_request_id_field_numbers_frozen() -> None:
    # 旧五 RPC 标签不可重编号；request_id 固定为单点请求 6、响应 7。
    request = commander_pb2.DESCRIPTOR.message_types_by_name["SubmitCommandRequest"]
    response = commander_pb2.DESCRIPTOR.message_types_by_name["SubmitCommandResponse"]
    assert request.fields_by_name["request_id"].number == 6
    assert response.fields_by_name["request_id"].number == 7


@pytest.mark.parametrize(
    "method",
    _all_methods(),
    ids=lambda method: method.full_name,
)
def test_rpc_name_io_types_and_unary(method: MethodDescriptor) -> None:
    input_name, output_name = EXPECTED_RPCS[method.full_name]
    assert method.input_type.full_name == input_name
    assert method.output_type.full_name == output_name
    # 50 个接口全部为 Unary，无 streaming。
    assert method.client_streaming is False
    assert method.server_streaming is False


def test_service_names_match_design() -> None:
    actual = {next(iter(module.DESCRIPTOR.services_by_name)) for module in EXPECTED}
    assert actual == {
        "CommonService",
        "CollectorService",
        "CommanderService",
        "SinkService",
        "IndustrialGatewayService",
        "DataGeneratorService",
    }
