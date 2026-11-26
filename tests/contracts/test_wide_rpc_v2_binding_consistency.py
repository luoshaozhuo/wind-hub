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


def test_all_v2_rpc_and_message_descriptors_match_frozen_contract() -> None:
    service = storage_pb.DESCRIPTOR.services_by_name["StorageService"]
    assert [method.name for method in service.methods] == [
        "QuerySamples",
        "QueryAggregates",
        "CreateExport",
        "GetExportTask",
        "CreateBackup",
        "GetBackupTask",
        "GetStorageStats",
        "ListBlockedPartitions",
        "RetryBlockedPartition",
    ]
    string = FieldDescriptor.TYPE_STRING
    message = FieldDescriptor.TYPE_MESSAGE
    enum = FieldDescriptor.TYPE_ENUM
    uint64 = FieldDescriptor.TYPE_UINT64
    uint32 = FieldDescriptor.TYPE_UINT32
    int64 = FieldDescriptor.TYPE_INT64
    layouts = {
        "StorageFilter": {
            "wind_farm_id": (1, string, False),
            "device_type": (2, string, False),
            "device_ids": (3, string, True),
            "point_table_id": (4, string, False),
            "data_level": (5, enum, False),
            "point_ids": (6, string, True),
            "start_time": (7, message, False),
            "end_time": (8, message, False),
            "qualities": (9, enum, True),
            "schema_fingerprint": (10, string, False),
        },
        "StorageValue": {
            "value_i64": (1, FieldDescriptor.TYPE_SINT64, False),
            "value_u64": (2, uint64, False),
            "value_f64": (3, FieldDescriptor.TYPE_DOUBLE, False),
            "value_bool": (4, FieldDescriptor.TYPE_BOOL, False),
            "value_text": (5, string, False),
            "payload_bytes": (6, FieldDescriptor.TYPE_BYTES, False),
        },
        "StorageColumn": {
            "point_id": (1, string, False),
            "business_point_id": (2, string, False),
            "unit": (3, string, False),
            "data_type": (4, string, False),
            "physical_name": (5, string, False),
        },
        "StorageWideRow": {
            "sample_time": (1, message, False),
            "values": (2, message, True),
            "quality": (3, FieldDescriptor.TYPE_BYTES, False),
        },
        "StorageGroup": {
            "wind_farm_id": (1, string, False),
            "device_id": (2, string, False),
            "device_type": (3, string, False),
            "point_table_id": (4, string, False),
            "data_level": (5, enum, False),
            "schema_fingerprint": (6, string, False),
            "columns": (7, message, True),
            "rows": (8, message, True),
        },
        "QuerySamplesRequest": {
            "filter": (1, message, False),
            "page_size": (2, uint32, False),
            "page_token": (3, string, False),
        },
        "QuerySamplesResponse": {
            "groups": (1, message, True),
            "next_page_token": (2, string, False),
            "complete": (3, FieldDescriptor.TYPE_BOOL, False),
        },
        "QueryAggregatesRequest": {
            "filter": (1, message, False),
            "aggregation": (2, enum, False),
            "window_ms": (3, uint64, False),
            "page_size": (4, uint32, False),
            "page_token": (5, string, False),
        },
        "StorageAggregate": {
            "window_start": (1, message, False),
            "window_end": (2, message, False),
            "wind_farm_id": (3, string, False),
            "device_id": (4, string, False),
            "point_table_id": (5, string, False),
            "data_level": (6, enum, False),
            "schema_fingerprint": (7, string, False),
            "column": (8, message, False),
            "good_count": (9, uint64, False),
            "missing_count": (10, uint64, False),
            "valid_count": (11, uint64, False),
            "result": (12, message, False),
        },
        "QueryAggregatesResponse": {
            "items": (1, message, True),
            "next_page_token": (2, string, False),
            "complete": (3, FieldDescriptor.TYPE_BOOL, False),
        },
        "CreateExportRequest": {
            "request_id": (1, string, False),
            "filter": (2, message, False),
            "export_format": (3, string, False),
        },
        "CreateBackupRequest": {
            "request_id": (1, string, False),
            "wind_farm_id": (2, string, False),
        },
        "StorageTask": {
            "task_id": (1, string, False),
            "state": (2, enum, False),
            "created_at": (3, message, False),
            "finished_at": (4, message, False),
            "artifact_id": (5, string, False),
            "size_bytes": (6, uint64, False),
            "sha256": (7, string, False),
            "error_code": (8, string, False),
            "message": (9, string, False),
            "artifact_path": (10, string, False),
            "integrity_verified": (11, FieldDescriptor.TYPE_BOOL, False),
        },
        "GetStorageTaskRequest": {"task_id": (1, string, False)},
        "GetStorageStatsRequest": {"wind_farm_id": (1, string, False)},
        "PartitionProgress": {
            "topic": (1, string, False),
            "partition": (2, FieldDescriptor.TYPE_INT32, False),
            "committed_next_offset": (3, int64, False),
            "last_confirmed_offset": (4, int64, False),
            "lag": (5, uint64, False),
            "block_reason": (6, string, False),
            "blocked_offset": (7, int64, False),
            "assignment_epoch": (8, string, False),
        },
        "GetStorageStatsResponse": {
            "partitions": (1, message, True),
            "persisted_points_total": (2, uint64, False),
            "write_unknown_total": (3, uint64, False),
            "schema_conflicts_total": (4, uint64, False),
            "active_queries": (5, uint32, False),
            "active_exports": (6, uint32, False),
            "active_backups": (7, uint32, False),
            "service_state": (8, string, False),
            "persisted_rows_total": (9, uint64, False),
            "confirmed_records_total": (10, uint64, False),
            "buffer_rows": (11, uint64, False),
            "buffer_bytes": (12, uint64, False),
            "replay_in_progress": (13, FieldDescriptor.TYPE_BOOL, False),
        },
        "ListBlockedPartitionsRequest": {
            "page_size": (1, uint32, False),
            "page_token": (2, string, False),
        },
        "ListBlockedPartitionsResponse": {
            "items": (1, message, True),
            "next_page_token": (2, string, False),
            "complete": (3, FieldDescriptor.TYPE_BOOL, False),
        },
        "RetryBlockedPartitionRequest": {
            "request_id": (1, string, False),
            "topic": (2, string, False),
            "partition": (3, FieldDescriptor.TYPE_INT32, False),
            "expected_blocked_offset": (4, int64, False),
        },
        "RetryBlockedPartitionResponse": {
            "accepted": (1, FieldDescriptor.TYPE_BOOL, False),
            "result": (2, string, False),
        },
    }
    for name, expected in layouts.items():
        fields = getattr(storage_pb, name).DESCRIPTOR.fields
        actual = {field.name: (field.number, field.type, field.is_repeated) for field in fields}
        assert actual == expected, name
    assert storage_pb.StorageFilter.DESCRIPTOR.fields_by_name["schema_fingerprint"].has_presence
    assert storage_pb.PartitionProgress.DESCRIPTOR.fields_by_name["blocked_offset"].has_presence
    oneof = storage_pb.StorageValue.DESCRIPTOR.oneofs_by_name["value"]
    assert {field.name for field in oneof.fields} == set(layouts["StorageValue"])
    assert storage_pb.StorageValue().WhichOneof("value") is None
    qualities = storage_pb.StorageQuality.DESCRIPTOR.values_by_name
    assert {name: value.number for name, value in qualities.items()} == {
        "STORAGE_QUALITY_NOT_COLLECTED": 0,
        "STORAGE_QUALITY_GOOD": 1,
        "STORAGE_QUALITY_BAD": 2,
        "STORAGE_QUALITY_UNCERTAIN": 3,
    }


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
