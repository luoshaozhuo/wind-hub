"""QueryWideRows generated wire contract and typed value fidelity."""

from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb


def test_wide_history_rpc_registered_with_exact_message_contract() -> None:
    service = pb.DESCRIPTOR.services_by_name["StorageService"]
    method = service.methods_by_name["QueryWideRows"]
    assert method.input_type.full_name == "windhub.storage.v1.QueryWideRowsRequest"
    assert method.output_type.full_name == "windhub.storage.v1.QueryWideRowsResponse"


def test_typed_wide_row_roundtrip_keeps_unsigned_null_and_quality() -> None:
    stamp = Timestamp(seconds=1795608000, nanos=1000)
    original = pb.QueryWideRowsResponse(
        wind_farm_id="farm",
        device_id="wtg1",
        device_config_id="device_farm_v1.0.0",
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=pb.STORAGE_DATA_LEVEL_PROTOCOL,
        schema_fingerprint="a" * 64,
        columns=[
            pb.WidePointColumn(
                point_id="unsigned",
                physical_column_name="p_unsigned",
                sql_type="BIGINT UNSIGNED",
                quality_ordinal=0,
            ),
            pb.WidePointColumn(
                point_id="missing",
                physical_column_name="p_missing",
                sql_type="FLOAT",
                quality_ordinal=1,
            ),
        ],
        rows=[
            pb.WideDeviceRow(
                sample_time=stamp,
                points=[
                    pb.WidePointValue(
                        point_id="unsigned",
                        quality=1,
                        unsigned_value=2**64 - 1,
                    ),
                    pb.WidePointValue(point_id="missing", quality=0),
                ],
            )
        ],
        complete=True,
    )
    decoded = pb.QueryWideRowsResponse.FromString(original.SerializeToString())
    assert decoded.rows[0].points[0].unsigned_value == 2**64 - 1
    assert decoded.rows[0].points[0].WhichOneof("value") == "unsigned_value"
    assert decoded.rows[0].points[1].WhichOneof("value") is None
    assert decoded.rows[0].points[1].quality == 0
