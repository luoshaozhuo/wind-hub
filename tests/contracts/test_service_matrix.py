"""41 个 RPC 的 Service Descriptor 矩阵（设计书 9.2-9.7 逐条核对）。"""

from __future__ import annotations

from types import ModuleType

import pytest
from google.protobuf.descriptor import MethodDescriptor

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
            ("GetConfigSnapshot", f"{C}GetConfigSnapshotRequest", f"{C}GetConfigSnapshotResponse"),
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
                "GetCommandResult",
                "windhub.commander.v1.GetCommandResultRequest",
                "windhub.commander.v1.GetCommandResultResponse",
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
            ("GetDevice", f"{C}ObjectId", "windhub.data_generator.v1.GetDeviceResponse"),
            (
                "ListScenarios",
                "windhub.data_generator.v1.ListScenariosRequest",
                "windhub.data_generator.v1.ListScenariosResponse",
            ),
            ("GetGeneration", f"{C}ObjectId", "windhub.data_generator.v1.GetGenerationResponse"),
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


def test_rpc_total_is_41() -> None:
    assert sum(len(rpcs) for _, _, rpcs in EXPECTED.values()) == 41
    assert len(_all_methods()) == 41


@pytest.mark.parametrize(
    "method",
    _all_methods(),
    ids=lambda method: method.full_name,
)
def test_rpc_name_io_types_and_unary(method: MethodDescriptor) -> None:
    input_name, output_name = EXPECTED_RPCS[method.full_name]
    assert method.input_type.full_name == input_name
    assert method.output_type.full_name == output_name
    # 设计书 9.2-9.7：41 个接口全部为 Unary，无 streaming。
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
