"""设计 §13.2 / §9：15 个 Server RPC 逐方法 Descriptor 断言。

DataGeneratorService 6 + CommonService 9，全部 Unary；校验真实入参/出参
消息类型、optional 字段、oneof 与枚举存在性。仅 Descriptor，不调用服务。
"""

from __future__ import annotations

import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import industrial_gateway_pb2 as gw

pytestmark = [pytest.mark.integration, pytest.mark.reliability, pytest.mark.contract]

DG_METHODS = {
    "ListDevices": (dg.ListDevicesRequest, dg.ListDevicesResponse),
    "GetDevice": (common.ObjectId, dg.GetDeviceResponse),
    "ListScenarios": (dg.ListScenariosRequest, dg.ListScenariosResponse),
    "GetGeneration": (common.ObjectId, dg.GetGenerationResponse),
    "StartGeneration": (dg.StartGenerationRequest, dg.StartGenerationResponse),
    "StopGeneration": (dg.StopGenerationRequest, dg.StopGenerationResponse),
}
COMMON_METHODS = {
    "GetServiceInfo": (common.Empty, common.GetServiceInfoResponse),
    "GetServiceStatus": (common.Empty, common.GetServiceStatusResponse),
    "GetCapabilities": (common.Empty, common.GetCapabilitiesResponse),
    "GetConfigSnapshot": (common.GetConfigSnapshotRequest, common.GetConfigSnapshotResponse),
    "PushConfigSnapshot": (common.PushConfigSnapshotRequest, common.PushConfigSnapshotResponse),
    "ApplyConfigSnapshot": (common.ApplyConfigSnapshotRequest, common.ApplyConfigSnapshotResponse),
    "GetMetrics": (common.GetMetricsRequest, common.GetMetricsResponse),
    "GetEvents": (common.GetEventsRequest, common.GetEventsResponse),
    "Diagnose": (common.DiagnoseRequest, common.DiagnoseResponse),
}


def test_data_generator_service_has_six_unary_methods() -> None:
    service = dg.DESCRIPTOR.services_by_name["DataGeneratorService"]
    assert {method.name for method in service.methods} == set(DG_METHODS)
    assert len(service.methods) == 6
    for method in service.methods:
        request_type, response_type = DG_METHODS[method.name]
        assert not method.client_streaming and not method.server_streaming
        assert method.input_type.full_name == request_type.DESCRIPTOR.full_name
        assert method.output_type.full_name == response_type.DESCRIPTOR.full_name


def test_common_service_has_nine_unary_methods() -> None:
    service = common.DESCRIPTOR.services_by_name["CommonService"]
    assert {method.name for method in service.methods} == set(COMMON_METHODS)
    assert len(service.methods) == 9
    for method in service.methods:
        request_type, response_type = COMMON_METHODS[method.name]
        assert not method.client_streaming and not method.server_streaming
        assert method.input_type.full_name == request_type.DESCRIPTOR.full_name
        assert method.output_type.full_name == response_type.DESCRIPTOR.full_name


def test_start_generation_scenario_version_is_proto_optional() -> None:
    fields = {field.name: field for field in dg.StartGenerationRequest.DESCRIPTOR.fields}
    assert fields["scenario_version"].has_presence
    request = dg.StartGenerationRequest(scenario_id="s")
    assert request.HasField("scenario_version") is False
    request.scenario_version = "1.0.0"
    assert request.HasField("scenario_version") is True


def test_sample_value_and_metadata_bounds_optional() -> None:
    sample_fields = {field.name: field for field in common.Sample.DESCRIPTOR.fields}
    assert sample_fields["value"].has_presence
    metadata = {field.name: field for field in common.PointMetadata.DESCRIPTOR.fields}
    assert metadata["minimum"].has_presence
    assert metadata["maximum"].has_presence


def test_scalar_value_is_oneof_named_kind() -> None:
    oneof = common.ScalarValue.DESCRIPTOR.oneofs_by_name["kind"]
    kind_names = {field.name for field in oneof.fields}
    assert {"sint64_value", "uint64_value", "double_value", "bool_value", "string_value", "bytes_value"} <= kind_names


def test_snapshot_scope_has_four_values() -> None:
    names = {value.name for value in common.SnapshotScope.DESCRIPTOR.values}
    assert names == {
        "SNAPSHOT_SCOPE_UNSPECIFIED",
        "SNAPSHOT_SCOPE_DISK",
        "SNAPSHOT_SCOPE_RUNTIME",
        "SNAPSHOT_SCOPE_CANDIDATE",
    }


def test_gateway_update_variables_contract_fields() -> None:
    request_fields = {field.name for field in gw.UpdateVariablesRequest.DESCRIPTOR.fields}
    assert request_fields == {"gateway_id", "values"}
    update_fields = {field.name for field in gw.VariableUpdate.DESCRIPTOR.fields}
    assert update_fields == {"variable_id", "value"}
    result_fields = {field.name for field in gw.VariableUpdateResult.DESCRIPTOR.fields}
    assert result_fields == {"variable_id", "success", "error_code", "message"}


def test_industrial_gateway_service_has_seven_unary_methods() -> None:
    service = gw.DESCRIPTOR.services_by_name["IndustrialGatewayService"]
    assert {method.name for method in service.methods} == {
        "ListGateways",
        "GetGateway",
        "StartGateway",
        "StopGateway",
        "ListVariables",
        "ReadVariables",
        "UpdateVariables",
    }
    assert all(
        not method.client_streaming and not method.server_streaming for method in service.methods
    )
