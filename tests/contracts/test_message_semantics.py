"""字段级语义：ScalarValue、optional/oneof、时间戳/分页/版本、快照、诊断、命令。

依据《CLI 软件设计 v1_0》9.1-9.7 的字段表达要求逐条正向验证。
"""

from __future__ import annotations

from types import ModuleType
from typing import Protocol

import pytest
from google.protobuf import timestamp_pb2
from google.protobuf.descriptor_pb2 import FieldDescriptorProto

from contracts import (
    collector_pb2,
    commander_pb2,
    common_pb2,
    data_generator_pb2,
    industrial_gateway_pb2,
    sink_service_pb2,
)

pytestmark = pytest.mark.contract


def test_scalar_value_all_six_kinds_roundtrip() -> None:
    cases = [
        ("bool_value", True),
        ("sint64_value", -123456789),
        ("uint64_value", 9_876_543_210),
        ("double_value", 3.14159),
        ("string_value", "机组-01"),
        ("bytes_value", b"\x00\x01\xfe\xff"),
    ]
    for field, value in cases:
        message = common_pb2.ScalarValue()
        setattr(message, field, value)
        assert message.WhichOneof("kind") == field
        decoded = common_pb2.ScalarValue()
        decoded.ParseFromString(message.SerializeToString())
        assert getattr(decoded, field) == value
        assert decoded.WhichOneof("kind") == field


def test_scalar_value_oneof_is_exclusive_and_default_unset() -> None:
    message = common_pb2.ScalarValue()
    assert message.WhichOneof("kind") is None
    message.sint64_value = 1
    message.string_value = "x"
    assert message.WhichOneof("kind") == "string_value"
    assert message.sint64_value == 0
    decoded = common_pb2.ScalarValue()
    decoded.ParseFromString(message.SerializeToString())
    assert decoded.WhichOneof("kind") == "string_value"


@pytest.mark.parametrize(
    ("message_type", "field", "zero"),
    [
        (common_pb2.PointMetadata, "minimum", 0.0),
        (common_pb2.PointMetadata, "maximum", 0.0),
        (collector_pb2.InterrogateDeviceRequest, "common_address", 0),
        (data_generator_pb2.StartGenerationRequest, "scenario_version", ""),
    ],
)
def test_optional_scalar_presence_distinguishes_unset(
    message_type: type, field: str, zero: object
) -> None:
    message = message_type()
    assert not message.HasField(field)
    # 设计书 9.1：缺省与零值需要区分时使用 optional；显式置零必须可识别。
    setattr(message, field, zero)
    assert message.HasField(field)
    decoded = message_type()
    decoded.ParseFromString(message.SerializeToString())
    assert decoded.HasField(field)
    message.ClearField(field)
    assert not message.HasField(field)


def test_sample_without_value_encodes_no_data_without_zero_substitution() -> None:
    # ReadPoints「不得用零替代无数据」：Sample.value 为 optional message，
    # 无数据点必须以未设置值 + 错误字段表达。
    sample = common_pb2.Sample(
        point_id="p1",
        quality="bad",
        error_code="NO_DATA",
        error_message="point not sampled",
    )
    assert not sample.HasField("value")
    decoded = common_pb2.Sample()
    decoded.ParseFromString(sample.SerializeToString())
    assert not decoded.HasField("value")
    assert decoded.error_code == "NO_DATA"

    # 反向：显式赋空 ScalarValue 必须保留 presence，与“无数据”区分。
    sample.value.CopyFrom(common_pb2.ScalarValue())
    present = common_pb2.Sample()
    present.ParseFromString(sample.SerializeToString())
    assert present.HasField("value")


def test_timestamp_and_temporal_fields_are_wire_compatible() -> None:
    info = common_pb2.GetServiceInfoResponse(
        started_at=timestamp_pb2.Timestamp(seconds=1_700_000_000, nanos=123_000_000)
    )
    decoded = common_pb2.GetServiceInfoResponse()
    decoded.ParseFromString(info.SerializeToString())
    assert decoded.started_at.seconds == 1_700_000_000
    assert decoded.started_at.nanos == 123_000_000

    cmd = commander_pb2.GetCommandResultResponse(
        updated_at=timestamp_pb2.Timestamp(seconds=1_700_000_001)
    )
    decoded_cmd = commander_pb2.GetCommandResultResponse()
    decoded_cmd.ParseFromString(cmd.SerializeToString())
    assert decoded_cmd.updated_at.seconds == 1_700_000_001


class _Paged(Protocol):
    @property
    def page_size(self) -> int: ...


def test_list_requests_and_responses_carry_pagination_contract() -> None:
    paged_requests: list[_Paged] = [
        collector_pb2.ListDevicesRequest(page_size=50, page_token="tok"),
        collector_pb2.ListPointsRequest(device_id="d", page_size=50),
        collector_pb2.ListTasksRequest(page_size=50),
        commander_pb2.ListDevicesRequest(page_size=50),
        commander_pb2.ListPointsRequest(device_id="d", page_size=50),
        sink_service_pb2.ListDeliveriesRequest(page_size=50),
        industrial_gateway_pb2.ListGatewaysRequest(page_size=50),
        industrial_gateway_pb2.ListVariablesRequest(gateway_id="g", page_size=50),
        data_generator_pb2.ListDevicesRequest(page_size=50),
        data_generator_pb2.ListScenariosRequest(page_size=50),
    ]
    for request in paged_requests:
        assert request.page_size == 50
    assert common_pb2.GetEventsRequest(page_size=100).page_size == 100

    assert collector_pb2.ListDevicesResponse(next_page_token="n").next_page_token == "n"
    assert common_pb2.GetEventsResponse(next_page_token="n").next_page_token == "n"


def test_revision_fields_present_on_all_mutating_requests() -> None:
    assert common_pb2.OperationRequest(id="t1", expected_revision="r1").expected_revision == "r1"
    assert common_pb2.PushConfigSnapshotRequest(expected_revision="r0").expected_revision == "r0"
    assert (
        common_pb2.ApplyConfigSnapshotRequest(
            candidate_id="c1", expected_revision="r0"
        ).expected_revision
        == "r0"
    )
    assert commander_pb2.SubmitCommandRequest(expected_revision="r2").expected_revision == "r2"
    assert (
        data_generator_pb2.StartGenerationRequest(
            scenario_id="s1", expected_revision="r3"
        ).expected_revision
        == "r3"
    )
    assert (
        data_generator_pb2.StopGenerationRequest(
            generation_id="g1", expected_revision="r3"
        ).expected_revision
        == "r3"
    )


def test_snapshot_messages_express_design_outputs() -> None:
    response = common_pb2.GetConfigSnapshotResponse(
        snapshot_id="snap-1",
        revision="r9",
        files=[
            common_pb2.SnapshotFile(
                relative_path="collector/collector.yaml",
                content=b"a: 1\n",
                size_bytes=5,
                sha256="a" * 64,
            )
        ],
        application_id="app-1",
        apply_results=[
            common_pb2.ApplicationResult(
                object_id="task-1", status="FAILED", error_code="RELOAD_DENIED", message="x"
            )
        ],
        last_error="task-1 rejected",
    )
    decoded = common_pb2.GetConfigSnapshotResponse()
    decoded.ParseFromString(response.SerializeToString())
    assert decoded.files[0].relative_path == "collector/collector.yaml"
    assert decoded.files[0].sha256 == "a" * 64
    assert decoded.application_id == "app-1"
    assert decoded.apply_results[0].status == "FAILED"
    assert decoded.last_error == "task-1 rejected"

    pushed = common_pb2.PushConfigSnapshotResponse(
        candidate_id="cand-1",
        valid=False,
        validation=[
            common_pb2.ValidationIssue(path="collector/a.yaml", code="BAD_PATH", message="..")
        ],
        base_revision="r9",
    )
    assert pushed.valid is False
    assert pushed.validation[0].code == "BAD_PATH"

    applied = common_pb2.ApplyConfigSnapshotResponse(
        file_commit="COMMITTED",
        apply_results=[common_pb2.ApplicationResult(object_id="o1", status="APPLIED")],
        effective_revision="r10",
        restart_required=True,
        application_id="app-2",
    )
    assert applied.restart_required is True
    assert applied.file_commit == "COMMITTED"


def test_snapshot_scope_enum_values_frozen() -> None:
    assert common_pb2.SNAPSHOT_SCOPE_UNSPECIFIED == 0
    assert common_pb2.SNAPSHOT_SCOPE_DISK == 1
    assert common_pb2.SNAPSHOT_SCOPE_RUNTIME == 2
    assert common_pb2.SNAPSHOT_SCOPE_CANDIDATE == 3


def test_diagnose_response_expresses_stage_evidence_time_source_limits() -> None:
    response = common_pb2.DiagnoseResponse(
        overall_status="FAILED",
        stages=[
            common_pb2.DiagnosticStage(
                stage="nats_push",
                status="FAILED",
                duration_ms=120,
                error_code="NO_MESSAGE_TRACE",
                message="missing message-level correlation",
                evidence=[
                    common_pb2.Evidence(name="stream", value="WH_COLLECTOR_01"),
                    common_pb2.Evidence(name="last_sequence", value="42"),
                ],
                observed_at=timestamp_pb2.Timestamp(seconds=1_700_000_002),
                source="collector",
            )
        ],
        limitations=["链路消息级关联证据缺失时不得认定端到端成功"],
    )
    decoded = common_pb2.DiagnoseResponse()
    decoded.ParseFromString(response.SerializeToString())
    stage = decoded.stages[0]
    assert stage.duration_ms == 120
    assert {e.name for e in stage.evidence} == {"stream", "last_sequence"}
    assert stage.observed_at.seconds == 1_700_000_002
    assert stage.source == "collector"
    assert decoded.limitations[0].startswith("链路")


def test_command_submit_and_result_fields() -> None:
    submit = commander_pb2.SubmitCommandResponse(
        command_id="cmd-1",
        accepted=True,
        state="EXECUTING",
        outcome="UNKNOWN",
    )
    decoded = commander_pb2.SubmitCommandResponse()
    decoded.ParseFromString(submit.SerializeToString())
    assert decoded.command_id == "cmd-1"
    assert decoded.outcome == "UNKNOWN"

    result = commander_pb2.GetCommandResultResponse(
        command_id="cmd-1",
        state="FINISHED",
        outcome="SUCCESS",
        device_id="d1",
        point_id="p1",
        updated_at=timestamp_pb2.Timestamp(seconds=1_700_000_010),
        io_settled=False,
    )
    decoded_result = commander_pb2.GetCommandResultResponse()
    decoded_result.ParseFromString(result.SerializeToString())
    assert decoded_result.io_settled is False
    assert decoded_result.outcome == "SUCCESS"


def test_command_request_carries_design_fields() -> None:
    request = commander_pb2.SubmitCommandRequest(
        device_id="d1",
        point_id="p1",
        value=common_pb2.ScalarValue(double_value=42.5),
        expires_at=timestamp_pb2.Timestamp(seconds=1_700_000_099),
        expected_revision="r1",
    )
    decoded = commander_pb2.SubmitCommandRequest()
    decoded.ParseFromString(request.SerializeToString())
    assert decoded.value.double_value == 42.5
    assert decoded.expires_at.seconds == 1_700_000_099


def test_cross_proto_references_resolve_to_common_messages() -> None:
    def message_full_name(module: ModuleType, message: str, name: str) -> str:
        descriptor = module.DESCRIPTOR.message_types_by_name[message].fields_by_name[name]
        assert descriptor.message_type is not None
        return str(descriptor.message_type.full_name)

    assert (
        message_full_name(collector_pb2, "ListPointsResponse", "points")
        == "windhub.common.v1.PointMetadata"
    )
    assert (
        message_full_name(collector_pb2, "ReadPointsResponse", "samples")
        == "windhub.common.v1.Sample"
    )
    assert (
        message_full_name(commander_pb2, "ListPointsResponse", "points")
        == "windhub.common.v1.PointMetadata"
    )
    assert (
        message_full_name(industrial_gateway_pb2, "ReadVariablesResponse", "values")
        == "windhub.common.v1.Sample"
    )
    assert (
        message_full_name(sink_service_pb2, "Delivery", "config")
        == "windhub.common.v1.AddressField"
    )


def test_readpoints_interrogate_express_per_point_outcomes() -> None:
    response = collector_pb2.ReadPointsResponse(
        device_id="d1",
        samples=[
            common_pb2.Sample(point_id="p1", quality="good", freshness="120ms"),
            common_pb2.Sample(point_id="p2", quality="bad", error_code="NO_DATA"),
        ],
    )
    decoded = collector_pb2.ReadPointsResponse()
    decoded.ParseFromString(response.SerializeToString())
    assert {s.point_id for s in decoded.samples} == {"p1", "p2"}

    interrogation = collector_pb2.InterrogateDeviceResponse(
        status="PARTIAL",
        samples=[common_pb2.Sample(point_id="p1", quality="good")],
        missing_point_ids=["p9"],
        error_code="",
    )
    assert interrogation.missing_point_ids == ["p9"]


def test_data_generator_scenario_and_generation_fields() -> None:
    scenario = data_generator_pb2.Scenario(
        scenario_id="sc-1",
        active_version="3",
        available_versions=["1", "2", "3"],
        device_ids=["vd-1", "vd-2"],
        description="昼峰夜谷",
    )
    decoded = data_generator_pb2.Scenario()
    decoded.ParseFromString(scenario.SerializeToString())
    assert decoded.active_version == "3"
    assert decoded.available_versions == ["1", "2", "3"]

    generation = data_generator_pb2.Generation(
        generation_id="gen-1",
        scenario_id="sc-1",
        scenario_version="3",
        desired_state="RUNNING",
        actual_state="RUNNING",
    )
    assert generation.scenario_version == "3"


def test_gateway_update_variables_batch_wire_roundtrip() -> None:
    """One Gateway request may report partial success without losing input order."""
    request = industrial_gateway_pb2.UpdateVariablesRequest(
        gateway_id="gateway-1",
        values=[
            industrial_gateway_pb2.VariableUpdate(
                variable_id="wind_power",
                value=common_pb2.ScalarValue(double_value=42.5),
            ),
            industrial_gateway_pb2.VariableUpdate(
                variable_id="missing",
                value=common_pb2.ScalarValue(bool_value=False),
            ),
        ],
    )
    restored = industrial_gateway_pb2.UpdateVariablesRequest.FromString(request.SerializeToString())
    assert restored.gateway_id == "gateway-1"
    assert [item.variable_id for item in restored.values] == ["wind_power", "missing"]
    assert restored.values[0].value.WhichOneof("kind") == "double_value"
    assert restored.values[1].value.WhichOneof("kind") == "bool_value"

    response = industrial_gateway_pb2.UpdateVariablesResponse(
        results=[
            industrial_gateway_pb2.VariableUpdateResult(variable_id="wind_power", success=True),
            industrial_gateway_pb2.VariableUpdateResult(
                variable_id="missing",
                success=False,
                error_code="NOT_FOUND",
                message="unknown variable",
            ),
        ]
    )
    decoded = industrial_gateway_pb2.UpdateVariablesResponse.FromString(
        response.SerializeToString()
    )
    assert [(item.variable_id, item.success) for item in decoded.results] == [
        ("wind_power", True),
        ("missing", False),
    ]
    assert decoded.results[1].error_code == "NOT_FOUND"


def test_gateway_update_variables_field_numbers_and_types_frozen() -> None:
    """v1_10 消息字段名/编号/类型冻结；UpdateVariablesRequest.lease_token=3 为围栏凭证。"""
    proto = FieldDescriptorProto

    def fields(
        message_name: str,
    ) -> list[tuple[int, str, int, str, str | None]]:
        message = industrial_gateway_pb2.DESCRIPTOR.message_types_by_name[message_name]
        return [
            (
                field.number,
                field.name,
                field.type,
                "repeated" if field.is_repeated else "singular",
                field.message_type.full_name if field.message_type is not None else None,
            )
            for field in message.fields
        ]

    assert fields("VariableUpdate") == [
        (1, "variable_id", proto.TYPE_STRING, "singular", None),
        (2, "value", proto.TYPE_MESSAGE, "singular", "windhub.common.v1.ScalarValue"),
    ]
    assert fields("UpdateVariablesRequest") == [
        (1, "gateway_id", proto.TYPE_STRING, "singular", None),
        (
            2,
            "values",
            proto.TYPE_MESSAGE,
            "repeated",
            "windhub.gateway.v1.VariableUpdate",
        ),
        (3, "lease_token", proto.TYPE_STRING, "singular", None),
    ]
    assert fields("ClaimTargetsRequest") == [
        (1, "gateway_id", proto.TYPE_STRING, "singular", None),
        (2, "owner_id", proto.TYPE_STRING, "singular", None),
        (3, "variable_ids", proto.TYPE_STRING, "repeated", None),
        (4, "action", proto.TYPE_STRING, "singular", None),
        (5, "lease_token", proto.TYPE_STRING, "singular", None),
        (6, "lease_ms", proto.TYPE_UINT32, "singular", None),
    ]
    assert fields("ClaimTargetsResponse") == [
        (1, "accepted", proto.TYPE_BOOL, "singular", None),
        (2, "lease_token", proto.TYPE_STRING, "singular", None),
        (3, "error_code", proto.TYPE_STRING, "singular", None),
        (4, "message", proto.TYPE_STRING, "singular", None),
    ]
    assert fields("VariableUpdateResult") == [
        (1, "variable_id", proto.TYPE_STRING, "singular", None),
        (2, "success", proto.TYPE_BOOL, "singular", None),
        (3, "error_code", proto.TYPE_STRING, "singular", None),
        (4, "message", proto.TYPE_STRING, "singular", None),
    ]
    assert fields("UpdateVariablesResponse") == [
        (
            1,
            "results",
            proto.TYPE_MESSAGE,
            "repeated",
            "windhub.gateway.v1.VariableUpdateResult",
        ),
    ]


def test_gateway_update_variables_shares_variable_id_semantics() -> None:
    """UpdateVariables 与 ListVariables/ReadVariables 使用同一字符串变量 ID 语义。"""
    descriptor = industrial_gateway_pb2.DESCRIPTOR
    listed = descriptor.message_types_by_name["Variable"].fields_by_name["variable_id"]
    read = descriptor.message_types_by_name["ReadVariablesRequest"].fields_by_name["variable_ids"]
    updated = descriptor.message_types_by_name["VariableUpdate"].fields_by_name["variable_id"]
    assert listed.type == updated.type == FieldDescriptorProto.TYPE_STRING
    assert read.type == FieldDescriptorProto.TYPE_STRING
    assert read.is_repeated
