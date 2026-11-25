"""Validate Protobuf v2 contracts against generated runtime and gRPC bindings."""

from unittest.mock import Mock

from google.protobuf.descriptor import FieldDescriptor

from contracts import (
    configuration_repository_pb2 as config_pb,
    storage_service_pb2 as storage_pb,
    storage_service_pb2_grpc as storage_rpc,
)


def test_query_wide_rows_grpc_endpoint_and_types_are_generated() -> None:
    service = storage_pb.DESCRIPTOR.services_by_name["StorageService"]
    method = service.methods_by_name["QueryWideRows"]
    assert method.input_type.full_name == "windhub.storage.v1.QueryWideRowsRequest"
    assert method.output_type.full_name == "windhub.storage.v1.QueryWideRowsResponse"

    channel = Mock()
    storage_rpc.StorageServiceStub(channel)
    routes = [call.args[0] for call in channel.unary_unary.call_args_list]
    assert routes.count("/windhub.storage.v1.StorageService/QueryWideRows") == 1


def test_wide_filter_types_and_versions_are_explicit() -> None:
    request = storage_pb.QueryWideRowsRequest.DESCRIPTOR.fields_by_name
    assert request["device_config_id"].number == 3
    assert request["data_level"].enum_type.name == "StorageDataLevel"
    assert request["point_ids"].is_repeated
    assert request["qualities"].is_repeated
    assert request["qualities"].type == FieldDescriptor.TYPE_UINT32
    assert request["qualities"].number == 10

    for parent, number in (
        (storage_pb.QueryAggregatesRequest, 6),
        (storage_pb.CreateExportRequest, 4),
    ):
        field = parent.DESCRIPTOR.fields_by_name["wide_filter"]
        assert field.number == number
        assert field.message_type is storage_pb.QueryWideRowsRequest.DESCRIPTOR


def test_wide_column_units_and_numeric_results_have_lossless_types() -> None:
    columns = storage_pb.WidePointColumn.DESCRIPTOR.fields_by_name
    assert columns["unit"].type == FieldDescriptor.TYPE_STRING
    values = storage_pb.WidePointValue.DESCRIPTOR.fields_by_name
    assert values["signed_value"].type == FieldDescriptor.TYPE_SINT64
    assert values["unsigned_value"].type == FieldDescriptor.TYPE_UINT64
    assert values["unsigned_value"].containing_oneof.name == "value"

    aggregates = storage_pb.StorageAggregate.DESCRIPTOR.fields_by_name
    assert aggregates["result_i64"].type == FieldDescriptor.TYPE_SINT64
    assert aggregates["result_u64"].type == FieldDescriptor.TYPE_UINT64
    assert aggregates["result_value_kind"].type == FieldDescriptor.TYPE_UINT32
    assert aggregates["result"].has_presence

    encoded = storage_pb.WidePointValue(
        point_id="counter", quality=1, unsigned_value=2**64 - 1
    ).SerializeToString()
    restored = storage_pb.WidePointValue.FromString(encoded)
    assert restored.WhichOneof("value") == "unsigned_value"
    assert restored.unsigned_value == 2**64 - 1

    empty = storage_pb.WidePointValue(point_id="counter", quality=0)
    assert empty.WhichOneof("value") is None


def test_config_exact_roots_are_immutable_version_references() -> None:
    field = config_pb.GetConfigBundleRequest.DESCRIPTOR.fields_by_name[
        "exact_roots"
    ]
    assert field.number == 4
    assert field.is_repeated
    assert field.message_type is config_pb.ConfigVersionRef.DESCRIPTOR

    original = config_pb.GetConfigBundleRequest(
        exact_roots=[
            config_pb.ConfigVersionRef(
                config_id=config_pb.ConfigId(
                    config_type="device", config_id="farm-a"
                ),
                version_id="immutable-version-id",
            )
        ],
        include_dependencies=True,
    )
    restored = config_pb.GetConfigBundleRequest.FromString(
        original.SerializeToString()
    )
    assert restored.exact_roots[0].version_id == "immutable-version-id"
    assert not restored.HasField("revision_id")
