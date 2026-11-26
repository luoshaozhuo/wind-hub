"""Validate Protobuf v2 contracts against generated runtime and gRPC bindings."""

from unittest.mock import Mock

from google.protobuf.descriptor import FieldDescriptor

from contracts import (
    configuration_repository_pb2 as config_pb,
)
from contracts import (
    storage_service_pb2 as storage_pb,
)
from contracts import (
    storage_service_pb2_grpc as storage_rpc,
)


def test_query_samples_grpc_endpoint_and_types_are_generated() -> None:
    service = storage_pb.DESCRIPTOR.services_by_name["StorageService"]
    method = service.methods_by_name["QuerySamples"]
    assert method.input_type.full_name == "windhub.storage.v2.QuerySamplesRequest"
    assert method.output_type.full_name == "windhub.storage.v2.QuerySamplesResponse"

    channel = Mock()
    storage_rpc.StorageServiceStub(channel)
    routes = [call.args[0] for call in channel.unary_unary.call_args_list]
    assert routes.count("/windhub.storage.v2.StorageService/QuerySamples") == 1


def test_wide_filter_types_and_versions_are_explicit() -> None:
    query = storage_pb.QuerySamplesRequest.DESCRIPTOR.fields_by_name
    assert query["filter"].number == 1
    assert query["filter"].message_type is storage_pb.StorageFilter.DESCRIPTOR

    request = storage_pb.StorageFilter.DESCRIPTOR.fields_by_name
    assert request["device_ids"].number == 3
    assert request["device_ids"].is_repeated
    assert request["data_level"].enum_type.name == "StorageDataLevel"
    assert request["point_ids"].is_repeated
    assert request["qualities"].is_repeated
    assert request["qualities"].type == FieldDescriptor.TYPE_ENUM
    assert request["qualities"].enum_type.name == "StorageQuality"
    assert request["qualities"].number == 9
    assert request["schema_fingerprint"].number == 10
    assert request["schema_fingerprint"].has_presence

    for parent, number in (
        (storage_pb.QueryAggregatesRequest, 1),
        (storage_pb.CreateExportRequest, 2),
    ):
        field = parent.DESCRIPTOR.fields_by_name["filter"]
        assert field.number == number
        assert field.message_type is storage_pb.StorageFilter.DESCRIPTOR


def test_wide_column_units_and_numeric_results_have_lossless_types() -> None:
    columns = storage_pb.StorageColumn.DESCRIPTOR.fields_by_name
    assert columns["unit"].type == FieldDescriptor.TYPE_STRING
    values = storage_pb.StorageValue.DESCRIPTOR.fields_by_name
    assert values["value_i64"].type == FieldDescriptor.TYPE_SINT64
    assert values["value_u64"].type == FieldDescriptor.TYPE_UINT64
    assert values["value_u64"].containing_oneof.name == "value"

    aggregates = storage_pb.StorageAggregate.DESCRIPTOR.fields_by_name
    assert aggregates["result"].number == 12
    assert aggregates["result"].message_type is storage_pb.StorageValue.DESCRIPTOR
    assert aggregates["result"].has_presence

    encoded = storage_pb.StorageValue(value_u64=2**64 - 1).SerializeToString()
    restored = storage_pb.StorageValue.FromString(encoded)
    assert restored.WhichOneof("value") == "value_u64"
    assert restored.value_u64 == 2**64 - 1

    empty = storage_pb.StorageValue()
    assert empty.WhichOneof("value") is None


def test_config_exact_roots_are_immutable_version_references() -> None:
    field = config_pb.GetConfigBundleRequest.DESCRIPTOR.fields_by_name["exact_roots"]
    assert field.number == 4
    assert field.is_repeated
    assert field.message_type is config_pb.ConfigVersionRef.DESCRIPTOR

    original = config_pb.GetConfigBundleRequest(
        exact_roots=[
            config_pb.ConfigVersionRef(
                config_id=config_pb.ConfigId(config_type="device", config_id="farm-a"),
                version_id="immutable-version-id",
            )
        ],
        include_dependencies=True,
    )
    restored = config_pb.GetConfigBundleRequest.FromString(original.SerializeToString())
    assert restored.exact_roots[0].version_id == "immutable-version-id"
    assert not restored.HasField("revision_id")
